"""每日忘却任务的 PostgreSQL owner lease 与阶段账本。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Never

if TYPE_CHECKING:
    from datetime import date

    from komari_bot.db.orm_connection import SharedEngineConnectionPool

type ForgettingJobStage = Literal[
    "claimed",
    "conversation_decay_done",
    "interaction_decay_done",
    "low_value_cleanup_done",
    "conversation_fuzzify_done",
    "completed",
]
type ForgettingJobClaimStatus = Literal["claimed", "busy", "completed"]
type SqlStageAction = tuple[str, tuple[object, ...]]

FORGETTING_JOB_NAME = "daily_forgetting"


@dataclass(frozen=True, slots=True)
class ForgettingJobClaim:
    """每日任务认领结果。"""

    status: ForgettingJobClaimStatus
    stage: ForgettingJobStage


class ForgettingJobLeaseLostError(RuntimeError):
    """每日忘却任务的 owner lease 已失效。"""

    def __init__(self, run_date: date) -> None:
        super().__init__(f"每日忘却任务租约已失效: run_date={run_date.isoformat()}")


class InvalidForgettingJobStageError(RuntimeError):
    """账本阶段与调用方预期不一致。"""

    def __init__(
        self,
        *,
        expected: ForgettingJobStage,
        actual: object,
    ) -> None:
        super().__init__(f"每日忘却任务阶段不一致: expected={expected}, actual={actual}")


def _raise_lease_lost(run_date: date) -> Never:
    raise ForgettingJobLeaseLostError(run_date)


def _normalize_stage(value: object) -> ForgettingJobStage:
    stage = str(value)
    match stage:
        case (
            "claimed"
            | "conversation_decay_done"
            | "interaction_decay_done"
            | "low_value_cleanup_done"
            | "conversation_fuzzify_done"
            | "completed"
        ):
            return stage
        case _:
            raise InvalidForgettingJobStageError(
                expected="claimed",
                actual=value,
            )


class ForgettingJobRepository:
    """使用数据库行锁保证同一天只由一个 worker 推进阶段。"""

    def __init__(self, pg_pool: SharedEngineConnectionPool) -> None:
        self._pg_pool = pg_pool

    async def claim(
        self,
        *,
        run_date: date,
        owner_token: str,
        lease_seconds: int,
    ) -> ForgettingJobClaim:
        """原子认领当天任务；完成或活跃租约不会再次执行。"""
        async with self._pg_pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                -- forgetting_job_insert
                INSERT INTO komari_memory_jobs (
                    job_name,
                    run_date,
                    owner_token,
                    lease_until,
                    stage,
                    attempt
                )
                VALUES (
                    $1,
                    $2,
                    $3,
                    NOW() + ($4 * INTERVAL '1 second'),
                    'claimed',
                    0
                )
                ON CONFLICT (job_name, run_date) DO NOTHING
                """,
                FORGETTING_JOB_NAME,
                run_date,
                owner_token,
                lease_seconds,
            )
            row = await conn.fetchrow(
                """
                -- forgetting_job_claim
                UPDATE komari_memory_jobs
                SET owner_token = $3,
                    lease_until = NOW() + ($4 * INTERVAL '1 second'),
                    attempt = attempt + 1,
                    last_error_code = NULL,
                    updated_at = NOW()
                WHERE job_name = $1
                  AND run_date = $2
                  AND stage <> 'completed'
                  AND (owner_token = $3 OR lease_until <= NOW())
                RETURNING stage
                """,
                FORGETTING_JOB_NAME,
                run_date,
                owner_token,
                lease_seconds,
            )
            if row is not None:
                return ForgettingJobClaim(
                    status="claimed",
                    stage=_normalize_stage(row["stage"]),
                )
            existing = await conn.fetchrow(
                """
                SELECT stage
                FROM komari_memory_jobs
                WHERE job_name = $1 AND run_date = $2
                """,
                FORGETTING_JOB_NAME,
                run_date,
            )
        if existing is None:
            _raise_lease_lost(run_date)
        stage = _normalize_stage(existing["stage"])
        return ForgettingJobClaim(
            status="completed" if stage == "completed" else "busy",
            stage=stage,
        )

    async def run_transactional_stage(
        self,
        *,
        run_date: date,
        owner_token: str,
        lease_seconds: int,
        expected_stage: ForgettingJobStage,
        next_stage: ForgettingJobStage,
        actions: tuple[SqlStageAction, ...],
    ) -> tuple[str, ...]:
        """在同一事务内执行业务 SQL 并推进账本，消除重复衰减窗口。"""
        async with self._pg_pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                -- forgetting_job_lock_stage
                SELECT stage
                FROM komari_memory_jobs
                WHERE job_name = $1
                  AND run_date = $2
                  AND owner_token = $3
                  AND lease_until > NOW()
                FOR UPDATE
                """,
                FORGETTING_JOB_NAME,
                run_date,
                owner_token,
            )
            if row is None:
                _raise_lease_lost(run_date)
            actual_stage = _normalize_stage(row["stage"])
            if actual_stage != expected_stage:
                raise InvalidForgettingJobStageError(
                    expected=expected_stage,
                    actual=actual_stage,
                )
            results = tuple(
                [await conn.execute(query, *params) for query, params in actions]
            )
            updated = await conn.fetchval(
                """
                -- forgetting_job_advance_in_transaction
                UPDATE komari_memory_jobs
                SET stage = $4,
                    lease_until = NOW() + ($5 * INTERVAL '1 second'),
                    updated_at = NOW(),
                    completed_at = CASE
                        WHEN $4 = 'completed' THEN NOW()
                        ELSE completed_at
                    END
                WHERE job_name = $1
                  AND run_date = $2
                  AND owner_token = $3
                  AND stage = $6
                RETURNING stage
                """,
                FORGETTING_JOB_NAME,
                run_date,
                owner_token,
                next_stage,
                lease_seconds,
                expected_stage,
            )
            if updated is None:
                _raise_lease_lost(run_date)
            return results

    async def advance_stage(
        self,
        *,
        run_date: date,
        owner_token: str,
        lease_seconds: int,
        expected_stage: ForgettingJobStage,
        next_stage: ForgettingJobStage,
    ) -> None:
        """推进已由逐记录 CAS 保证幂等的长耗时阶段。"""
        async with self._pg_pool.acquire() as conn:
            updated = await conn.fetchval(
                """
                -- forgetting_job_advance
                UPDATE komari_memory_jobs
                SET stage = $4,
                    lease_until = NOW() + ($5 * INTERVAL '1 second'),
                    updated_at = NOW(),
                    completed_at = CASE
                        WHEN $4 = 'completed' THEN NOW()
                        ELSE completed_at
                    END
                WHERE job_name = $1
                  AND run_date = $2
                  AND owner_token = $3
                  AND lease_until > NOW()
                  AND stage = $6
                RETURNING stage
                """,
                FORGETTING_JOB_NAME,
                run_date,
                owner_token,
                next_stage,
                lease_seconds,
                expected_stage,
            )
        if updated is None:
            _raise_lease_lost(run_date)

    async def renew(
        self,
        *,
        run_date: date,
        owner_token: str,
        lease_seconds: int,
    ) -> bool:
        """续租未完成任务。"""
        async with self._pg_pool.acquire() as conn:
            updated = await conn.fetchval(
                """
                -- forgetting_job_renew
                UPDATE komari_memory_jobs
                SET lease_until = NOW() + ($4 * INTERVAL '1 second'),
                    updated_at = NOW()
                WHERE job_name = $1
                  AND run_date = $2
                  AND owner_token = $3
                  AND stage <> 'completed'
                RETURNING stage
                """,
                FORGETTING_JOB_NAME,
                run_date,
                owner_token,
                lease_seconds,
            )
        return updated is not None

    async def mark_failure(
        self,
        *,
        run_date: date,
        owner_token: str,
        error_code: str,
    ) -> None:
        """记录稳定错误码，不覆盖已被新 owner 接管的任务。"""
        async with self._pg_pool.acquire() as conn:
            await conn.execute(
                """
                -- forgetting_job_failure
                UPDATE komari_memory_jobs
                SET last_error_code = $4,
                    updated_at = NOW()
                WHERE job_name = $1
                  AND run_date = $2
                  AND owner_token = $3
                  AND stage <> 'completed'
                """,
                FORGETTING_JOB_NAME,
                run_date,
                owner_token,
                error_code,
            )
