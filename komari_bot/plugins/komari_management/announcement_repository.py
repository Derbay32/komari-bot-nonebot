"""维护公告请求的跨 worker 幂等与冷却账本。"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from komari_bot.common.database_config import get_shared_database_config
from komari_bot.common.postgres import create_postgres_pool

if TYPE_CHECKING:
    import asyncpg

type DispatchClaimState = Literal[
    "claimed",
    "replay",
    "in_progress",
    "payload_conflict",
    "cooldown",
    "reconciliation_required",
]

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS komari_announcement_dispatches (
    request_id TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('processing', 'completed', 'reconciliation_required')),
    owner_token TEXT,
    lease_expires_at TIMESTAMPTZ,
    response_payload JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_komari_announcement_dispatches_created_at
ON komari_announcement_dispatches (created_at DESC);
"""
_ADVISORY_LOCK_ID = 6_126_613_117_029_977_126


@dataclass(frozen=True, slots=True)
class DispatchClaim:
    """一次公告请求账本抢占结果。"""

    state: DispatchClaimState
    response_payload: dict[str, Any] | None = None
    remaining_seconds: float | None = None


class AnnouncementDispatchRepository:
    """PostgreSQL 公告幂等账本。"""

    def __init__(self) -> None:
        self._pool: asyncpg.Pool | None = None
        self._initialize_lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self._pool is not None:
            return
        async with self._initialize_lock:
            if self._pool is not None:
                return
            pool = await create_postgres_pool(
                get_shared_database_config(),
                command_timeout=30,
            )
            try:
                async with pool.acquire() as connection:
                    await connection.execute(_SCHEMA_SQL)
            except Exception:
                await pool.close()
                raise
            self._pool = pool

    async def close(self) -> None:
        async with self._initialize_lock:
            pool = self._pool
            self._pool = None
            if pool is not None:
                await pool.close()

    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            message = "公告幂等账本尚未初始化"
            raise RuntimeError(message)
        return self._pool

    @staticmethod
    def _decode_response(value: object) -> dict[str, Any] | None:
        if value is None:
            return None
        payload = json.loads(value) if isinstance(value, str) else value
        return payload if isinstance(payload, dict) else None

    async def claim(  # noqa: PLR0911 - 状态机的每个结果都需要明确返回
        self,
        *,
        request_id: str,
        payload_hash: str,
        owner_token: str,
        lease_seconds: int,
        cooldown_seconds: float,
    ) -> DispatchClaim:
        """原子校验 request ID、全局冷却并抢占新公告请求。"""
        await self.initialize()
        pool = self._require_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock($1)",
                _ADVISORY_LOCK_ID,
            )
            await connection.execute(
                """
                DELETE FROM komari_announcement_dispatches
                WHERE created_at < CURRENT_TIMESTAMP - INTERVAL '30 days'
                """
            )
            existing = await connection.fetchrow(
                """
                SELECT payload_hash,
                       status,
                       response_payload,
                       lease_expires_at
                FROM komari_announcement_dispatches
                WHERE request_id = $1
                FOR UPDATE
                """,
                request_id,
            )
            if existing is not None:
                if str(existing["payload_hash"]) != payload_hash:
                    return DispatchClaim(state="payload_conflict")
                status = str(existing["status"])
                if status == "completed":
                    response = self._decode_response(existing["response_payload"])
                    if response is None:
                        return DispatchClaim(state="reconciliation_required")
                    return DispatchClaim(state="replay", response_payload=response)
                if status == "reconciliation_required":
                    return DispatchClaim(state="reconciliation_required")

                expired = await connection.fetchval(
                    "SELECT $1::timestamptz <= CURRENT_TIMESTAMP",
                    existing["lease_expires_at"],
                )
                if not bool(expired):
                    return DispatchClaim(state="in_progress")
                await connection.execute(
                    """
                    UPDATE komari_announcement_dispatches
                    SET status = 'reconciliation_required',
                        owner_token = NULL,
                        lease_expires_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE request_id = $1
                    """,
                    request_id,
                )
                return DispatchClaim(state="reconciliation_required")

            if cooldown_seconds > 0:
                remaining = await connection.fetchval(
                    """
                    SELECT GREATEST(
                        $1::double precision
                        - EXTRACT(
                            EPOCH FROM (CURRENT_TIMESTAMP - created_at)
                        ),
                        0
                    )
                    FROM komari_announcement_dispatches
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    cooldown_seconds,
                )
                if remaining is not None and float(remaining) > 0:
                    return DispatchClaim(
                        state="cooldown",
                        remaining_seconds=float(remaining),
                    )

            await connection.execute(
                """
                INSERT INTO komari_announcement_dispatches (
                    request_id,
                    payload_hash,
                    status,
                    owner_token,
                    lease_expires_at
                )
                VALUES (
                    $1,
                    $2,
                    'processing',
                    $3,
                    CURRENT_TIMESTAMP
                    + ($4::double precision * INTERVAL '1 second')
                )
                """,
                request_id,
                payload_hash,
                owner_token,
                lease_seconds,
            )
        return DispatchClaim(state="claimed")

    async def complete(
        self,
        *,
        request_id: str,
        owner_token: str,
        response_payload: dict[str, Any],
    ) -> bool:
        """仅由当前 owner 持久化最终响应并完成请求。"""
        pool = self._require_pool()
        async with pool.acquire() as connection:
            completed = await connection.fetchval(
                """
                UPDATE komari_announcement_dispatches
                SET status = 'completed',
                    response_payload = $3::jsonb,
                    owner_token = NULL,
                    lease_expires_at = NULL,
                    completed_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE request_id = $1
                  AND status = 'processing'
                  AND owner_token = $2
                  AND lease_expires_at > CURRENT_TIMESTAMP
                RETURNING request_id
                """,
                request_id,
                owner_token,
                json.dumps(
                    response_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        return completed is not None

    async def mark_reconciliation_required(
        self,
        *,
        request_id: str,
        owner_token: str,
    ) -> None:
        """异常退出时阻止该 request ID 被自动重发。"""
        pool = self._require_pool()
        async with pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE komari_announcement_dispatches
                SET status = 'reconciliation_required',
                    owner_token = NULL,
                    lease_expires_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE request_id = $1
                  AND status = 'processing'
                  AND owner_token = $2
                """,
                request_id,
                owner_token,
            )

    async def cancel_unstarted(
        self,
        *,
        request_id: str,
        owner_token: str,
    ) -> bool:
        """在确认尚未调用平台发送接口时删除当前抢占。"""
        pool = self._require_pool()
        async with pool.acquire() as connection:
            deleted = await connection.fetchval(
                """
                DELETE FROM komari_announcement_dispatches
                WHERE request_id = $1
                  AND status = 'processing'
                  AND owner_token = $2
                RETURNING request_id
                """,
                request_id,
                owner_token,
            )
        return deleted is not None


@dataclass(slots=True)
class _MemoryDispatch:
    payload_hash: str
    status: Literal["processing", "completed", "reconciliation_required"]
    owner_token: str | None
    lease_expires_at: float | None
    response_payload: dict[str, Any] | None
    created_at: float


class InMemoryAnnouncementDispatchRepository:
    """路由单元测试使用的同契约内存账本。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._records: dict[str, _MemoryDispatch] = {}

    async def claim(  # noqa: PLR0911 - 状态机的每个结果都需要明确返回
        self,
        *,
        request_id: str,
        payload_hash: str,
        owner_token: str,
        lease_seconds: int,
        cooldown_seconds: float,
    ) -> DispatchClaim:
        async with self._lock:
            now = time.monotonic()
            existing = self._records.get(request_id)
            if existing is not None:
                if existing.payload_hash != payload_hash:
                    return DispatchClaim(state="payload_conflict")
                if existing.status == "completed":
                    return DispatchClaim(
                        state="replay",
                        response_payload=existing.response_payload,
                    )
                if existing.status == "reconciliation_required":
                    return DispatchClaim(state="reconciliation_required")
                if existing.lease_expires_at is not None and existing.lease_expires_at > now:
                    return DispatchClaim(state="in_progress")
                existing.status = "reconciliation_required"
                existing.owner_token = None
                existing.lease_expires_at = None
                return DispatchClaim(state="reconciliation_required")

            if self._records and cooldown_seconds > 0:
                latest = max(item.created_at for item in self._records.values())
                remaining = cooldown_seconds - (now - latest)
                if remaining > 0:
                    return DispatchClaim(
                        state="cooldown",
                        remaining_seconds=remaining,
                    )
            self._records[request_id] = _MemoryDispatch(
                payload_hash=payload_hash,
                status="processing",
                owner_token=owner_token,
                lease_expires_at=now + lease_seconds,
                response_payload=None,
                created_at=now,
            )
            return DispatchClaim(state="claimed")

    async def complete(
        self,
        *,
        request_id: str,
        owner_token: str,
        response_payload: dict[str, Any],
    ) -> bool:
        async with self._lock:
            record = self._records.get(request_id)
            if (
                record is None
                or record.status != "processing"
                or record.owner_token != owner_token
                or record.lease_expires_at is None
                or record.lease_expires_at <= time.monotonic()
            ):
                return False
            record.status = "completed"
            record.owner_token = None
            record.lease_expires_at = None
            record.response_payload = dict(response_payload)
            return True

    async def mark_reconciliation_required(
        self,
        *,
        request_id: str,
        owner_token: str,
    ) -> None:
        async with self._lock:
            record = self._records.get(request_id)
            if (
                record is not None
                and record.status == "processing"
                and record.owner_token == owner_token
            ):
                record.status = "reconciliation_required"
                record.owner_token = None
                record.lease_expires_at = None

    async def cancel_unstarted(
        self,
        *,
        request_id: str,
        owner_token: str,
    ) -> bool:
        async with self._lock:
            record = self._records.get(request_id)
            if (
                record is None
                or record.status != "processing"
                or record.owner_token != owner_token
            ):
                return False
            del self._records[request_id]
            return True


_repository = AnnouncementDispatchRepository()


def get_announcement_dispatch_repository() -> AnnouncementDispatchRepository:
    return _repository


async def close_announcement_dispatch_repository() -> None:
    await _repository.close()
