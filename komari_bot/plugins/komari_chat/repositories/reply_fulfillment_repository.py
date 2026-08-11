"""回复履约父子存储 adapter。

本模块只提供 TSK-79 的持久化边界，正常聊天路径仍由旧版回复提交
仓库负责。冻结领域值对象与纯构建函数统一复用内部
``reply_fulfillment_domain``；承诺载荷在进入 JSONB 前必须经过固定
值对象校验与序列化。
"""

from __future__ import annotations

import json
from typing import Any

from ..reply_fulfillment_domain import (
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
        """把待确认送达的回复推进为已送达。"""
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
        return changed is not None

    async def mark_not_delivered(self, fulfillment_id: str) -> bool:
        """把待确认送达的回复推进为未送达终态。"""
        async with self.pg_pool.acquire() as connection:
            changed = await connection.fetchval(
                """
                UPDATE komari_chat_reply_fulfillments
                SET delivery_state = 'NOT_DELIVERED',
                    not_delivered_at = COALESCE(not_delivered_at, NOW()),
                    updated_at = NOW()
                WHERE fulfillment_id = $1
                  AND delivery_state = 'PENDING_CONFIRMATION'
                RETURNING fulfillment_id
                """,
                fulfillment_id,
            )
        return changed is not None

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
    ) -> str | None:
        """记录一个承诺的独立失败、退避或耗尽状态。"""
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
                                3600
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
