"""聊天回复送达后副作用的 PostgreSQL outbox。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    import asyncpg

ReplyCommitStep = Literal[
    "proactive_confirmed",
    "favorability_applied",
    "ai_history_stored",
    "interaction_stored",
]

_STEP_COLUMNS: dict[ReplyCommitStep, str] = {
    "proactive_confirmed": "proactive_confirmed_at",
    "favorability_applied": "favorability_applied_at",
    "ai_history_stored": "ai_history_stored_at",
    "interaction_stored": "interaction_stored_at",
}

_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS komari_chat_reply_commit_outbox (
        operation_id TEXT PRIMARY KEY,
        payload_hash TEXT NOT NULL,
        request_trace_id TEXT NOT NULL,
        source_message_id TEXT NOT NULL,
        platform_message_id TEXT,
        group_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        user_nickname TEXT,
        bot_nickname TEXT,
        reply_content TEXT,
        reply_timestamp DOUBLE PRECISION NOT NULL,
        favorability_delta INT NOT NULL,
        favorability_reason TEXT,
        interaction_history JSONB,
        proactive_reservation_id TEXT,
        proactive_cooldown_seconds INT NOT NULL CHECK (
            proactive_cooldown_seconds >= 0
        ),
        global_interaction_enabled BOOLEAN NOT NULL,
        global_interaction_trigger_size INT NOT NULL CHECK (
            global_interaction_trigger_size > 0
        ),
        status TEXT NOT NULL CHECK (
            status IN (
                'PREPARED', 'DELIVERED', 'PROCESSING',
                'COMPLETED', 'CANCELLED', 'FAILED'
            )
        ),
        proactive_confirmed_at TIMESTAMPTZ,
        favorability_applied_at TIMESTAMPTZ,
        ai_history_stored_at TIMESTAMPTZ,
        interaction_stored_at TIMESTAMPTZ,
        attempt_count INT NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
        next_retry_at TIMESTAMPTZ,
        lease_owner TEXT,
        lease_expires_at TIMESTAMPTZ,
        last_error_code TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        delivered_at TIMESTAMPTZ,
        completed_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_komari_chat_reply_commit_claim
    ON komari_chat_reply_commit_outbox(
        status, next_retry_at, lease_expires_at, created_at
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_komari_chat_reply_commit_cleanup
    ON komari_chat_reply_commit_outbox(status, completed_at)
    """,
)


@dataclass(frozen=True)
class PendingReplyCommit:
    """发送前持久化的待提交回复载荷。"""

    operation_id: str
    request_trace_id: str
    source_message_id: str
    group_id: str
    user_id: str
    user_nickname: str
    bot_nickname: str
    reply_content: str
    reply_timestamp: float
    favorability_delta: int
    favorability_reason: str | None
    interaction_history: dict[str, str]
    proactive_reservation_id: str | None
    proactive_cooldown_seconds: int
    global_interaction_enabled: bool
    global_interaction_trigger_size: int

    def payload_hash(self) -> str:
        """计算载荷指纹，用于检测 operation ID 碰撞。"""
        payload = {
            "operation_id": self.operation_id,
            "request_trace_id": self.request_trace_id,
            "source_message_id": self.source_message_id,
            "group_id": self.group_id,
            "user_id": self.user_id,
            "user_nickname": self.user_nickname,
            "bot_nickname": self.bot_nickname,
            "reply_content": self.reply_content,
            "reply_timestamp": self.reply_timestamp,
            "favorability_delta": self.favorability_delta,
            "favorability_reason": self.favorability_reason,
            "interaction_history": self.interaction_history,
            "proactive_reservation_id": self.proactive_reservation_id,
            "proactive_cooldown_seconds": self.proactive_cooldown_seconds,
            "global_interaction_enabled": self.global_interaction_enabled,
            "global_interaction_trigger_size": self.global_interaction_trigger_size,
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ReplyCommitRepository:
    """回复副作用 outbox 的租约与状态机仓库。"""

    def __init__(self, pg_pool: asyncpg.Pool) -> None:
        self.pg_pool = pg_pool
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()

    async def ensure_schema(self) -> None:
        """单飞创建 outbox 表结构。"""
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            async with self.pg_pool.acquire() as connection:
                for statement in _SCHEMA_STATEMENTS:
                    await connection.execute(statement)
            self._schema_ready = True

    async def has_active_operation(self, operation_id: str) -> bool:
        """判断同一平台事件是否已有不可重发的意图或 tombstone。"""
        await self.ensure_schema()
        async with self.pg_pool.acquire() as connection:
            exists = await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM komari_chat_reply_commit_outbox
                    WHERE operation_id = $1
                      AND status <> 'CANCELLED'
                )
                """,
                operation_id,
            )
        return bool(exists)

    async def prepare(self, payload: PendingReplyCommit) -> bool:
        """插入发送意图；活动 operation 已存在时返回 False。"""
        await self.ensure_schema()
        payload_hash = payload.payload_hash()
        async with self.pg_pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO komari_chat_reply_commit_outbox (
                    operation_id, payload_hash, request_trace_id,
                    source_message_id, group_id, user_id, user_nickname,
                    bot_nickname, reply_content,
                    reply_timestamp,
                    favorability_delta, favorability_reason, interaction_history,
                    proactive_reservation_id, proactive_cooldown_seconds,
                    global_interaction_enabled, global_interaction_trigger_size,
                    status
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                    $11, $12, $13::jsonb, $14, $15, $16, $17, 'PREPARED'
                )
                ON CONFLICT (operation_id) DO UPDATE
                SET payload_hash = EXCLUDED.payload_hash,
                    request_trace_id = EXCLUDED.request_trace_id,
                    source_message_id = EXCLUDED.source_message_id,
                    platform_message_id = NULL,
                    group_id = EXCLUDED.group_id,
                    user_id = EXCLUDED.user_id,
                    user_nickname = EXCLUDED.user_nickname,
                    bot_nickname = EXCLUDED.bot_nickname,
                    reply_content = EXCLUDED.reply_content,
                    reply_timestamp = EXCLUDED.reply_timestamp,
                    favorability_delta = EXCLUDED.favorability_delta,
                    favorability_reason = EXCLUDED.favorability_reason,
                    interaction_history = EXCLUDED.interaction_history,
                    proactive_reservation_id = EXCLUDED.proactive_reservation_id,
                    proactive_cooldown_seconds = EXCLUDED.proactive_cooldown_seconds,
                    global_interaction_enabled = EXCLUDED.global_interaction_enabled,
                    global_interaction_trigger_size = EXCLUDED.global_interaction_trigger_size,
                    status = 'PREPARED',
                    proactive_confirmed_at = NULL,
                    favorability_applied_at = NULL,
                    ai_history_stored_at = NULL,
                    interaction_stored_at = NULL,
                    attempt_count = 0,
                    next_retry_at = NULL,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    last_error_code = NULL,
                    delivered_at = NULL,
                    completed_at = NULL,
                    updated_at = NOW()
                WHERE komari_chat_reply_commit_outbox.status = 'CANCELLED'
                RETURNING operation_id
                """,
                payload.operation_id,
                payload_hash,
                payload.request_trace_id,
                payload.source_message_id,
                payload.group_id,
                payload.user_id,
                payload.user_nickname,
                payload.bot_nickname,
                payload.reply_content,
                payload.reply_timestamp,
                payload.favorability_delta,
                payload.favorability_reason,
                json.dumps(payload.interaction_history, ensure_ascii=False),
                payload.proactive_reservation_id,
                payload.proactive_cooldown_seconds,
                payload.global_interaction_enabled,
                payload.global_interaction_trigger_size,
            )
        return row is not None

    async def cancel_prepared(self, operation_id: str) -> bool:
        """发送失败时只取消尚未确认送达的意图并清除正文。"""
        await self.ensure_schema()
        async with self.pg_pool.acquire() as connection:
            cancelled = await connection.fetchval(
                """
                UPDATE komari_chat_reply_commit_outbox
                SET status = 'CANCELLED',
                    bot_nickname = NULL,
                    user_nickname = NULL,
                    reply_content = NULL,
                    favorability_reason = NULL,
                    interaction_history = NULL,
                    proactive_reservation_id = NULL,
                    platform_message_id = NULL,
                    updated_at = NOW()
                WHERE operation_id = $1
                  AND status = 'PREPARED'
                RETURNING operation_id
                """,
                operation_id,
            )
        return cancelled is not None

    async def mark_delivered(
        self,
        operation_id: str,
        *,
        platform_message_id: str | None = None,
    ) -> bool:
        """把 PREPARED 意图原子转换为可领取的 DELIVERED 任务。"""
        await self.ensure_schema()
        async with self.pg_pool.acquire() as connection:
            delivered = await connection.fetchval(
                """
                UPDATE komari_chat_reply_commit_outbox
                SET status = 'DELIVERED',
                    platform_message_id = COALESCE(platform_message_id, $2),
                    delivered_at = COALESCE(delivered_at, NOW()),
                    next_retry_at = NULL,
                    updated_at = NOW()
                WHERE operation_id = $1
                  AND status = 'PREPARED'
                RETURNING operation_id
                """,
                operation_id,
                platform_message_id,
            )
            if delivered is not None:
                return True
            existing = await connection.fetchrow(
                """
                SELECT status, platform_message_id
                FROM komari_chat_reply_commit_outbox
                WHERE operation_id = $1
                """,
                operation_id,
            )
        if existing is None:
            return False
        existing_platform_id = existing["platform_message_id"]
        if (
            platform_message_id is not None
            and existing_platform_id is not None
            and str(existing_platform_id) != platform_message_id
        ):
            msg = "回复 operation 对应的平台消息 ID 冲突"
            raise RuntimeError(msg)
        return existing["status"] in {
            "DELIVERED",
            "PROCESSING",
            "COMPLETED",
            "FAILED",
        }

    async def claim_operation(
        self,
        operation_id: str,
        *,
        owner_token: str,
        lease_seconds: int,
    ) -> dict[str, Any] | None:
        """领取单个已送达任务，并回收其过期租约。"""
        await self.ensure_schema()
        async with self.pg_pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE komari_chat_reply_commit_outbox
                SET status = 'PROCESSING',
                    lease_owner = $2,
                    lease_expires_at = NOW() + ($3 * INTERVAL '1 second'),
                    attempt_count = attempt_count + 1,
                    next_retry_at = NULL,
                    updated_at = NOW()
                WHERE operation_id = $1
                  AND (
                      (
                          status = 'DELIVERED'
                          AND (next_retry_at IS NULL OR next_retry_at <= NOW())
                      )
                      OR (
                          status = 'PROCESSING'
                          AND lease_expires_at <= NOW()
                      )
                  )
                RETURNING *
                """,
                operation_id,
                owner_token,
                max(1, lease_seconds),
            )
        return dict(row) if row else None

    async def claim_pending(
        self,
        *,
        owner_token: str,
        limit: int,
        lease_seconds: int,
    ) -> list[dict[str, Any]]:
        """以 SKIP LOCKED 批量领取到期任务。"""
        if limit <= 0:
            return []
        await self.ensure_schema()
        async with self.pg_pool.acquire() as connection, connection.transaction():
            rows = await connection.fetch(
                """
                WITH candidates AS (
                    SELECT operation_id
                    FROM komari_chat_reply_commit_outbox
                    WHERE (
                        (
                            status = 'DELIVERED'
                            AND (next_retry_at IS NULL OR next_retry_at <= NOW())
                        )
                        OR (
                            status = 'PROCESSING'
                            AND lease_expires_at <= NOW()
                        )
                    )
                    ORDER BY COALESCE(next_retry_at, delivered_at, created_at),
                             operation_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT $1
                )
                UPDATE komari_chat_reply_commit_outbox outbox
                SET status = 'PROCESSING',
                    lease_owner = $2,
                    lease_expires_at = NOW() + ($3 * INTERVAL '1 second'),
                    attempt_count = outbox.attempt_count + 1,
                    next_retry_at = NULL,
                    updated_at = NOW()
                FROM candidates
                WHERE outbox.operation_id = candidates.operation_id
                RETURNING outbox.*
                """,
                limit,
                owner_token,
                max(1, lease_seconds),
            )
        return [dict(row) for row in rows]

    async def renew_lease(
        self,
        operation_id: str,
        *,
        owner_token: str,
        lease_seconds: int,
    ) -> bool:
        """续期当前 worker 持有的处理租约。"""
        async with self.pg_pool.acquire() as connection:
            renewed = await connection.fetchval(
                """
                UPDATE komari_chat_reply_commit_outbox
                SET lease_expires_at = NOW() + ($3 * INTERVAL '1 second'),
                    updated_at = NOW()
                WHERE operation_id = $1
                  AND status = 'PROCESSING'
                  AND lease_owner = $2
                RETURNING operation_id
                """,
                operation_id,
                owner_token,
                max(1, lease_seconds),
            )
        return renewed is not None

    async def mark_step(
        self,
        operation_id: str,
        *,
        owner_token: str,
        step: ReplyCommitStep,
    ) -> bool:
        """由租约 owner 幂等确认一个子步骤完成。"""
        column = _STEP_COLUMNS[step]
        async with self.pg_pool.acquire() as connection:
            marked = await connection.fetchval(
                f"""
                UPDATE komari_chat_reply_commit_outbox
                SET {column} = COALESCE({column}, NOW()),
                    updated_at = NOW()
                WHERE operation_id = $1
                  AND status = 'PROCESSING'
                  AND lease_owner = $2
                RETURNING operation_id
                """,
                operation_id,
                owner_token,
            )
        return marked is not None

    async def complete(self, operation_id: str, *, owner_token: str) -> bool:
        """全部步骤完成后提交 tombstone，并清除敏感载荷。"""
        async with self.pg_pool.acquire() as connection:
            completed = await connection.fetchval(
                """
                UPDATE komari_chat_reply_commit_outbox
                SET status = 'COMPLETED',
                    bot_nickname = NULL,
                    user_nickname = NULL,
                    reply_content = NULL,
                    favorability_reason = NULL,
                    interaction_history = NULL,
                    proactive_reservation_id = NULL,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    next_retry_at = NULL,
                    last_error_code = NULL,
                    completed_at = COALESCE(completed_at, NOW()),
                    updated_at = NOW()
                WHERE operation_id = $1
                  AND status = 'PROCESSING'
                  AND lease_owner = $2
                  AND proactive_confirmed_at IS NOT NULL
                  AND favorability_applied_at IS NOT NULL
                  AND ai_history_stored_at IS NOT NULL
                  AND interaction_stored_at IS NOT NULL
                RETURNING operation_id
                """,
                operation_id,
                owner_token,
            )
        return completed is not None

    async def mark_failure(
        self,
        operation_id: str,
        *,
        owner_token: str,
        error_code: str,
        max_attempts: int,
        retry_base_seconds: int,
    ) -> str | None:
        """记录无正文错误码，按指数退避重试并在上限后转 FAILED。"""
        async with self.pg_pool.acquire() as connection:
            status = await connection.fetchval(
                """
                UPDATE komari_chat_reply_commit_outbox
                SET status = CASE
                        WHEN attempt_count >= $4 THEN 'FAILED'
                        ELSE 'DELIVERED'
                    END,
                    next_retry_at = CASE
                        WHEN attempt_count >= $4 THEN NULL
                        ELSE NOW() + (
                            LEAST(
                                $5 * POWER(2, GREATEST(attempt_count - 1, 0)),
                                3600
                            ) * INTERVAL '1 second'
                        )
                    END,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    last_error_code = LEFT($3, 100),
                    updated_at = NOW()
                WHERE operation_id = $1
                  AND status = 'PROCESSING'
                  AND lease_owner = $2
                RETURNING status
                """,
                operation_id,
                owner_token,
                error_code,
                max(1, max_attempts),
                max(1, retry_base_seconds),
            )
        return str(status) if status is not None else None

    async def cleanup_tombstones(self, *, retention_days: int) -> int:
        """清理过期的 COMPLETED/CANCELLED 防重记录。"""
        await self.ensure_schema()
        async with self.pg_pool.acquire() as connection:
            result = await connection.execute(
                """
                DELETE FROM komari_chat_reply_commit_outbox
                WHERE status IN ('COMPLETED', 'CANCELLED')
                  AND updated_at < NOW() - ($1 * INTERVAL '1 day')
                """,
                max(1, retention_days),
            )
        return int(result.split()[-1])


__all__ = ["PendingReplyCommit", "ReplyCommitRepository", "ReplyCommitStep"]
