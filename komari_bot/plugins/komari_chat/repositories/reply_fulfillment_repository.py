"""回复履约父子存储 adapter。

本模块只提供 TSK-79 的持久化边界，正常聊天路径仍由旧版回复提交
仓库负责。冻结领域值对象与纯构建函数统一复用内部
``reply_fulfillment_domain``；承诺载荷在进入 JSONB 前必须经过固定
值对象校验与序列化。
"""

from __future__ import annotations

import json
from contextlib import suppress
from typing import Any

from ..reply_fulfillment_domain import (
    _COMMITMENT_PAYLOAD_TYPES,
    COMMITMENT_TYPES,
    AssistantReplyHistoryPayload,
    CommitmentPayload,
    FavorabilityAdjustmentPayload,
    InteractionHistoryPayload,
    ProactiveReplyConfirmationPayload,
    ReplyCommitmentInput,
    ReplyFulfillmentConflictError,
    ReplyFulfillmentDraft,
)

# 承诺类型到冻结领域值对象的固定映射，与领域模块保持单一事实来源。
_COMMITMENT_ORDER = {
    commitment_type: index for index, commitment_type in enumerate(COMMITMENT_TYPES)
}


class ReplyFulfillmentRepository:
    """回复履约父子记录的 PostgreSQL adapter。"""

    def __init__(self, pg_pool: Any) -> None:
        self.pg_pool = pg_pool

    async def prepare(self, draft: ReplyFulfillmentDraft) -> bool:
        """在一个事务中插入一个父记录及其固定适用子项。"""
        async with self.pg_pool.acquire() as connection, connection.transaction():
            inserted = await connection.fetchval(
                """
                INSERT INTO komari_chat_reply_fulfillments (
                    fulfillment_id,
                    payload_hash,
                    request_trace_id,
                    trigger_message_id,
                    trigger_user_id,
                    group_id,
                    bot_self_id,
                    adapter_name,
                    reply_target_message_id,
                    reply_content,
                    delivery_state
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                    'NOT_STARTED'
                )
                ON CONFLICT (fulfillment_id) DO NOTHING
                RETURNING fulfillment_id
                """,
                draft.fulfillment_id,
                draft.payload_hash,
                draft.request_trace_id,
                draft.trigger_message_id,
                draft.trigger_user_id,
                draft.group_id,
                draft.bot_self_id,
                draft.adapter_name,
                draft.reply_target_message_id,
                draft.reply_content,
            )
            if inserted is None:
                existing_hash = await connection.fetchval(
                    """
                    SELECT payload_hash
                    FROM komari_chat_reply_fulfillments
                    WHERE fulfillment_id = $1
                    FOR SHARE
                    """,
                    draft.fulfillment_id,
                )
                if existing_hash is None:
                    msg = "回复履约准备后未读取到持久身份"
                    raise RuntimeError(msg)
                if str(existing_hash) != draft.payload_hash:
                    msg = f"履约冲突: {draft.fulfillment_id}"
                    raise ReplyFulfillmentConflictError(msg)
                return False

            await connection.executemany(
                """
                INSERT INTO komari_chat_reply_fulfillment_commitments (
                    fulfillment_id,
                    commitment_type,
                    payload
                )
                VALUES ($1, $2, $3::jsonb)
                """,
                [
                    (
                        draft.fulfillment_id,
                        commitment.commitment_type,
                        json.dumps(
                            commitment.to_json(),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    )
                    for commitment in draft.commitments
                ],
            )
        return True

    async def mark_send_started(self, fulfillment_id: str) -> bool:
        """把回复从未发送推进到待确认送达。"""
        async with self.pg_pool.acquire() as connection:
            changed = await connection.fetchval(
                """
                UPDATE komari_chat_reply_fulfillments
                SET delivery_state = 'PENDING_CONFIRMATION',
                    send_started_at = COALESCE(send_started_at, NOW()),
                    updated_at = NOW()
                WHERE fulfillment_id = $1
                  AND delivery_state = 'NOT_STARTED'
                RETURNING fulfillment_id
                """,
                fulfillment_id,
            )
        return changed is not None

    async def mark_delivered(
        self,
        fulfillment_id: str,
        *,
        platform_message_id: str | None = None,
    ) -> bool:
        """把待确认送达的回复推进为已送达。

        同一平台消息 ID 可重复确认（幂等）；冲突平台消息 ID 明确抛错，
        不得覆盖既有送达事实。
        """
        async with self.pg_pool.acquire() as connection:
            changed = await connection.fetchval(
                """
                UPDATE komari_chat_reply_fulfillments
                SET delivery_state = 'DELIVERED',
                    platform_message_id = COALESCE(
                        platform_message_id,
                        $2
                    ),
                    delivered_at = COALESCE(delivered_at, NOW()),
                    updated_at = NOW()
                WHERE fulfillment_id = $1
                  AND delivery_state = 'PENDING_CONFIRMATION'
                RETURNING fulfillment_id
                """,
                fulfillment_id,
                platform_message_id,
            )
            if changed is not None:
                return True
            existing = await connection.fetchrow(
                """
                SELECT delivery_state, platform_message_id
                FROM komari_chat_reply_fulfillments
                WHERE fulfillment_id = $1
                """,
                fulfillment_id,
            )
        if existing is None:
            return False
        if existing["delivery_state"] == "DELIVERED":
            existing_platform_id = existing["platform_message_id"]
            if (
                platform_message_id is not None
                and existing_platform_id is not None
                and str(existing_platform_id) != platform_message_id
            ):
                msg = "平台消息 ID 冲突"
                raise ValueError(msg)
            return True
        return False

    async def mark_not_delivered(self, fulfillment_id: str) -> bool:
        """明确失败或时效终止：回复进入未送达互斥终态。

        允许从未发送直接进入未送达（发送开始前过期），此时
        ``send_started_at`` 保持 NULL。
        """
        async with self.pg_pool.acquire() as connection:
            changed = await connection.fetchval(
                """
                UPDATE komari_chat_reply_fulfillments
                SET delivery_state = 'NOT_DELIVERED',
                    not_delivered_at = COALESCE(not_delivered_at, NOW()),
                    updated_at = NOW()
                WHERE fulfillment_id = $1
                  AND delivery_state IN ('NOT_STARTED', 'PENDING_CONFIRMATION')
                RETURNING fulfillment_id
                """,
                fulfillment_id,
            )
        return changed is not None

    async def has_active_operation(self, fulfillment_id: str) -> bool:
        """履约身份一旦持久化，就阻止同一事件再次发送。"""
        async with self.pg_pool.acquire() as connection:
            found = await connection.fetchval(
                """
                SELECT 1
                FROM komari_chat_reply_fulfillments
                WHERE fulfillment_id = $1
                """,
                fulfillment_id,
            )
        return found is not None

    async def claim_fresh_not_started(
        self,
        *,
        bot_self_id: str,
        adapter_name: str,
        freshness_seconds: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        """原子领取原 Bot 身份匹配、仍在时效内的未发送履约。

        只有 ``bot_self_id`` 与 ``adapter_name`` 精确匹配且
        ``prepared_at`` 距今未满时效的 NOT_STARTED 行才会被领取；
        领取即登记发送开始（``PENDING_CONFIRMATION``）。
        """
        if limit <= 0:
            return []
        async with self.pg_pool.acquire() as connection, connection.transaction():
            rows = await connection.fetch(
                """
                WITH candidates AS (
                    SELECT fulfillment_id
                    FROM komari_chat_reply_fulfillments
                    WHERE delivery_state = 'NOT_STARTED'
                      AND bot_self_id = $1
                      AND adapter_name = $2
                      AND prepared_at > NOW() - ($3 * INTERVAL '1 second')
                    ORDER BY prepared_at, fulfillment_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT $4
                )
                UPDATE komari_chat_reply_fulfillments AS parent
                SET delivery_state = 'PENDING_CONFIRMATION',
                    send_started_at = COALESCE(send_started_at, NOW()),
                    updated_at = NOW()
                FROM candidates
                WHERE parent.fulfillment_id = candidates.fulfillment_id
                RETURNING parent.*
                """,
                bot_self_id,
                adapter_name,
                max(1, freshness_seconds),
                limit,
            )
        return [dict(row) for row in rows]

    async def expire_stale_not_started(
        self,
        *,
        freshness_seconds: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        """终止超过时效仍未发送的履约，返回被终止行供调用方释放预占。

        满时效（``prepared_at`` 距今 >= 时效）的 NOT_STARTED 行转为
        未送达终态，``send_started_at`` 保持 NULL；未送达回复永不重发。
        """
        if limit <= 0:
            return []
        async with self.pg_pool.acquire() as connection, connection.transaction():
            rows = await connection.fetch(
                """
                WITH candidates AS (
                    SELECT fulfillment_id
                    FROM komari_chat_reply_fulfillments
                    WHERE delivery_state = 'NOT_STARTED'
                      AND prepared_at <= NOW() - ($1 * INTERVAL '1 second')
                    ORDER BY prepared_at, fulfillment_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT $2
                )
                UPDATE komari_chat_reply_fulfillments AS parent
                SET delivery_state = 'NOT_DELIVERED',
                    not_delivered_at = COALESCE(not_delivered_at, NOW()),
                    updated_at = NOW()
                FROM candidates
                WHERE parent.fulfillment_id = candidates.fulfillment_id
                RETURNING parent.*
                """,
                max(1, freshness_seconds),
                limit,
            )
        return [dict(row) for row in rows]

    async def claim_operation(
        self,
        fulfillment_id: str,
        *,
        owner_token: str,
        lease_seconds: int,
    ) -> dict[str, Any] | None:
        """领取单个到期履约，并原子回收过期父租约。"""
        async with self.pg_pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE komari_chat_reply_fulfillments AS parent
                SET lease_owner = $2,
                    lease_expires_at = NOW() + ($3 * INTERVAL '1 second'),
                    updated_at = NOW()
                WHERE parent.fulfillment_id = $1
                  AND parent.delivery_state = 'DELIVERED'
                  AND parent.completed_at IS NULL
                  AND (
                      parent.lease_owner IS NULL
                      OR parent.lease_expires_at <= NOW()
                  )
                  AND EXISTS (
                      SELECT 1
                      FROM komari_chat_reply_fulfillment_commitments AS child
                      WHERE child.fulfillment_id = parent.fulfillment_id
                        AND (
                            child.state = 'PENDING'
                            OR (
                                child.state = 'RETRY_WAIT'
                                AND child.next_retry_at <= NOW()
                            )
                        )
                  )
                RETURNING parent.*
                """,
                fulfillment_id,
                owner_token,
                max(1, lease_seconds),
            )
        return dict(row) if row is not None else None

    async def claim_pending(
        self,
        *,
        owner_token: str,
        limit: int,
        lease_seconds: int,
    ) -> list[dict[str, Any]]:
        """按稳定顺序批量领取待处理履约，并跳过已锁父记录。"""
        if limit <= 0:
            return []
        async with self.pg_pool.acquire() as connection, connection.transaction():
            rows = await connection.fetch(
                """
                WITH candidates AS (
                    SELECT parent.fulfillment_id
                    FROM komari_chat_reply_fulfillments AS parent
                    WHERE parent.delivery_state = 'DELIVERED'
                      AND parent.completed_at IS NULL
                      AND (
                          parent.lease_owner IS NULL
                          OR parent.lease_expires_at <= NOW()
                      )
                      AND EXISTS (
                          SELECT 1
                          FROM komari_chat_reply_fulfillment_commitments AS child
                          WHERE child.fulfillment_id = parent.fulfillment_id
                            AND (
                                child.state = 'PENDING'
                                OR (
                                    child.state = 'RETRY_WAIT'
                                    AND child.next_retry_at <= NOW()
                                )
                            )
                      )
                    ORDER BY
                        COALESCE(parent.delivered_at, parent.prepared_at),
                        parent.fulfillment_id
                    FOR UPDATE OF parent SKIP LOCKED
                    LIMIT $1
                )
                UPDATE komari_chat_reply_fulfillments AS parent
                SET lease_owner = $2,
                    lease_expires_at = NOW() + ($3 * INTERVAL '1 second'),
                    updated_at = NOW()
                FROM candidates
                WHERE parent.fulfillment_id = candidates.fulfillment_id
                RETURNING parent.*
                """,
                limit,
                owner_token,
                max(1, lease_seconds),
            )
        claimed = [dict(row) for row in rows]
        return sorted(
            claimed,
            key=lambda row: (
                row["delivered_at"] or row["prepared_at"],
                str(row["fulfillment_id"]),
            ),
        )

    async def renew_lease(
        self,
        fulfillment_id: str,
        *,
        owner_token: str,
        lease_seconds: int,
    ) -> bool:
        """仅允许当前且未过期 owner 续租。"""
        async with self.pg_pool.acquire() as connection:
            renewed = await connection.fetchval(
                """
                UPDATE komari_chat_reply_fulfillments
                SET lease_expires_at = NOW() + ($3 * INTERVAL '1 second'),
                    updated_at = NOW()
                WHERE fulfillment_id = $1
                  AND lease_owner = $2
                  AND lease_expires_at > NOW()
                  AND completed_at IS NULL
                RETURNING fulfillment_id
                """,
                fulfillment_id,
                owner_token,
                max(1, lease_seconds),
            )
        return renewed is not None

    async def release_lease(self, fulfillment_id: str, *, owner_token: str) -> bool:
        """仅允许当前且未过期 owner 释放父租约。"""
        async with self.pg_pool.acquire() as connection:
            released = await connection.fetchval(
                """
                UPDATE komari_chat_reply_fulfillments
                SET lease_owner = NULL,
                    lease_expires_at = NULL,
                    updated_at = NOW()
                WHERE fulfillment_id = $1
                  AND lease_owner = $2
                  AND lease_expires_at > NOW()
                  AND completed_at IS NULL
                RETURNING fulfillment_id
                """,
                fulfillment_id,
                owner_token,
            )
        return released is not None

    async def load_claimed_commitments(
        self,
        fulfillment_id: str,
        *,
        owner_token: str,
    ) -> list[dict[str, Any]] | None:
        """在有效父 owner 下加载当前到期承诺及其经领域校验的载荷。

        只返回 ``PENDING`` 或已到退避时间的 ``RETRY_WAIT`` 子项；父
        租约不再属于 ``owner_token``（丢失或被回收）时返回 None，执行
        器据此立即中止本轮。asyncpg 默认返回的 JSONB 文本先解码，再经
        冻结领域值对象校验并规范化；解码或校验失败的损坏载荷保持原样
        返回，由执行器按 ``invalid_payload`` 独立处置。返回顺序按
        ``COMMITMENT_TYPES`` 稳定排序，不依赖存储返回顺序。
        """
        async with self.pg_pool.acquire() as connection:
            owned = await connection.fetchval(
                """
                SELECT 1
                FROM komari_chat_reply_fulfillments
                WHERE fulfillment_id = $1
                  AND lease_owner = $2
                  AND lease_expires_at > NOW()
                  AND completed_at IS NULL
                """,
                fulfillment_id,
                owner_token,
            )
            if owned is None:
                return None
            rows = await connection.fetch(
                """
                SELECT commitment_type, payload
                FROM komari_chat_reply_fulfillment_commitments
                WHERE fulfillment_id = $1
                  AND (
                      state = 'PENDING'
                      OR (state = 'RETRY_WAIT' AND next_retry_at <= NOW())
                  )
                """,
                fulfillment_id,
            )
        commitments: list[dict[str, Any]] = []
        for row in rows:
            commitment_type = str(row["commitment_type"])
            payload_type = _COMMITMENT_PAYLOAD_TYPES[commitment_type]
            canonical: Any = row["payload"]
            if isinstance(canonical, str):
                # asyncpg 默认把 JSONB 返回为文本，先解码再进入领域校验。
                with suppress(TypeError, ValueError):
                    canonical = json.loads(canonical)
            if isinstance(canonical, dict):
                with suppress(TypeError, ValueError):
                    canonical = payload_type(**canonical).to_json()
            commitments.append(
                {
                    "commitment_type": commitment_type,
                    "payload": canonical,
                }
            )
        return sorted(
            commitments,
            key=lambda item: _COMMITMENT_ORDER.get(
                str(item["commitment_type"]), len(_COMMITMENT_ORDER)
            ),
        )

    async def mark_commitment_completed(
        self,
        fulfillment_id: str,
        *,
        commitment_type: str,
        owner_token: str,
    ) -> bool:
        """由有效父 owner 独立完成一个承诺，并消费其敏感载荷。"""
        async with self.pg_pool.acquire() as connection:
            completed = await connection.fetchval(
                """
                UPDATE komari_chat_reply_fulfillment_commitments AS child
                SET state = 'COMPLETED',
                    attempt_count = child.attempt_count + 1,
                    next_retry_at = NULL,
                    last_error_code = NULL,
                    payload = NULL,
                    completed_at = COALESCE(child.completed_at, NOW()),
                    updated_at = NOW()
                FROM komari_chat_reply_fulfillments AS parent
                WHERE child.fulfillment_id = $1
                  AND child.commitment_type = $2
                  AND child.state IN ('PENDING', 'RETRY_WAIT')
                  AND parent.fulfillment_id = child.fulfillment_id
                  AND parent.delivery_state = 'DELIVERED'
                  AND parent.completed_at IS NULL
                  AND parent.lease_owner = $3
                  AND parent.lease_expires_at > NOW()
                RETURNING child.fulfillment_id
                """,
                fulfillment_id,
                commitment_type,
                owner_token,
            )
        return completed is not None

    async def mark_commitment_failed(
        self,
        fulfillment_id: str,
        *,
        commitment_type: str,
        owner_token: str,
        error_code: str,
        max_attempts: int,
        retry_base_seconds: int,
        retry_max_seconds: int = 3600,
    ) -> str | None:
        """记录一个承诺的独立失败、退避或耗尽状态。

        ``retry_max_seconds`` 是执行器动态传入的退避上限；不写死 3600，
        也不写入冻结载荷。
        """
        async with self.pg_pool.acquire() as connection:
            state = await connection.fetchval(
                """
                UPDATE komari_chat_reply_fulfillment_commitments AS child
                SET state = CASE
                        WHEN child.attempt_count + 1 >= $4 THEN 'FAILED'
                        ELSE 'RETRY_WAIT'
                    END,
                    attempt_count = child.attempt_count + 1,
                    next_retry_at = CASE
                        WHEN child.attempt_count + 1 >= $4 THEN NULL
                        ELSE NOW() + (
                            LEAST(
                                $5 * POWER(2, GREATEST(child.attempt_count, 0)),
                                $7
                            ) * INTERVAL '1 second'
                        )
                    END,
                    last_error_code = LEFT($3, 100),
                    completed_at = NULL,
                    updated_at = NOW()
                FROM komari_chat_reply_fulfillments AS parent
                WHERE child.fulfillment_id = $1
                  AND child.commitment_type = $2
                  AND child.state IN ('PENDING', 'RETRY_WAIT')
                  AND parent.fulfillment_id = child.fulfillment_id
                  AND parent.delivery_state = 'DELIVERED'
                  AND parent.completed_at IS NULL
                  AND parent.lease_owner = $6
                  AND parent.lease_expires_at > NOW()
                RETURNING child.state
                """,
                fulfillment_id,
                commitment_type,
                error_code,
                max(1, max_attempts),
                max(1, retry_base_seconds),
                owner_token,
                max(1, retry_max_seconds),
            )
        return str(state) if state is not None else None

    async def complete_fulfillment(
        self,
        fulfillment_id: str,
        *,
        owner_token: str,
    ) -> bool:
        """仅在所有适用承诺完成后完成父履约并清除租约。"""
        async with self.pg_pool.acquire() as connection:
            completed = await connection.fetchval(
                """
                UPDATE komari_chat_reply_fulfillments AS parent
                SET completed_at = COALESCE(parent.completed_at, NOW()),
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    updated_at = NOW()
                WHERE parent.fulfillment_id = $1
                  AND parent.delivery_state = 'DELIVERED'
                  AND parent.completed_at IS NULL
                  AND parent.lease_owner = $2
                  AND parent.lease_expires_at > NOW()
                  AND NOT EXISTS (
                      SELECT 1
                      FROM komari_chat_reply_fulfillment_commitments AS child
                      WHERE child.fulfillment_id = parent.fulfillment_id
                        AND child.state <> 'COMPLETED'
                  )
                RETURNING parent.fulfillment_id
                """,
                fulfillment_id,
                owner_token,
            )
        return completed is not None


__all__ = [
    "COMMITMENT_TYPES",
    "AssistantReplyHistoryPayload",
    "CommitmentPayload",
    "FavorabilityAdjustmentPayload",
    "InteractionHistoryPayload",
    "ProactiveReplyConfirmationPayload",
    "ReplyCommitmentInput",
    "ReplyFulfillmentConflictError",
    "ReplyFulfillmentDraft",
    "ReplyFulfillmentRepository",
]
