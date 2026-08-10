"""回复履约父子存储 adapter。

本模块只提供 TSK-79 的持久化边界，正常聊天路径仍由旧版回复提交
仓库负责。承诺载荷在进入 JSONB 前必须经过本模块的固定 dataclass
校验与序列化。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

COMMITMENT_TYPES: tuple[str, ...] = (
    "proactive_reply_confirmation",
    "favorability_adjustment",
    "assistant_reply_history",
    "interaction_history",
)


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        msg = f"{field_name} 必须是字符串"
        raise TypeError(msg)
    if not value:
        msg = f"{field_name} 不能为空"
        raise ValueError(msg)
    return value


def _require_int(value: object, field_name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{field_name} 必须是整数"
        raise TypeError(msg)
    if minimum is not None and value < minimum:
        msg = f"{field_name} 必须大于等于 {minimum}"
        raise ValueError(msg)
    return value


def _require_finite_float(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        msg = f"{field_name} 必须是数字"
        raise TypeError(msg)
    result = float(value)
    if not math.isfinite(result):
        msg = f"{field_name} 必须是有限数字"
        raise ValueError(msg)
    return result


@dataclass(frozen=True, slots=True)
class ProactiveReplyConfirmationPayload:
    """主动回复确认承诺的固定载荷。"""

    group_id: str
    reservation_id: str
    cooldown_seconds: int

    def __post_init__(self) -> None:
        _require_text(self.group_id, "group_id")
        _require_text(self.reservation_id, "reservation_id")
        _require_int(self.cooldown_seconds, "cooldown_seconds", minimum=0)

    def to_json(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "reservation_id": self.reservation_id,
            "cooldown_seconds": self.cooldown_seconds,
        }


@dataclass(frozen=True, slots=True)
class FavorabilityAdjustmentPayload:
    """好感度调整承诺的固定载荷。"""

    user_id: str
    delta: int
    reason: str

    def __post_init__(self) -> None:
        _require_text(self.user_id, "user_id")
        _require_int(self.delta, "delta")
        _require_text(self.reason, "reason")

    def to_json(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "delta": self.delta,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class AssistantReplyHistoryPayload:
    """角色回复历史承诺的固定载荷。"""

    group_id: str
    bot_nickname: str
    reply_content: str
    reply_timestamp: float

    def __post_init__(self) -> None:
        _require_text(self.group_id, "group_id")
        _require_text(self.bot_nickname, "bot_nickname")
        _require_text(self.reply_content, "reply_content")
        _require_finite_float(self.reply_timestamp, "reply_timestamp")

    def to_json(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "bot_nickname": self.bot_nickname,
            "reply_content": self.reply_content,
            "reply_timestamp": self.reply_timestamp,
        }


@dataclass(frozen=True, slots=True)
class InteractionHistoryPayload:
    """互动历史承诺的固定载荷。"""

    user_id: str
    trigger_size: int
    record: dict[str, str]

    def __post_init__(self) -> None:
        _require_text(self.user_id, "user_id")
        _require_int(self.trigger_size, "trigger_size", minimum=1)
        if not isinstance(self.record, dict):
            msg = "record 必须是字符串键值字典"
            raise TypeError(msg)
        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.record.items()
        ):
            msg = "record 必须是字符串键值字典"
            raise TypeError(msg)
        object.__setattr__(self, "record", dict(self.record))

    def to_json(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "trigger_size": self.trigger_size,
            "record": dict(self.record),
        }


CommitmentPayload = (
    ProactiveReplyConfirmationPayload
    | FavorabilityAdjustmentPayload
    | AssistantReplyHistoryPayload
    | InteractionHistoryPayload
)

_COMMITMENT_PAYLOAD_TYPES: dict[str, type[Any]] = {
    "proactive_reply_confirmation": ProactiveReplyConfirmationPayload,
    "favorability_adjustment": FavorabilityAdjustmentPayload,
    "assistant_reply_history": AssistantReplyHistoryPayload,
    "interaction_history": InteractionHistoryPayload,
}


@dataclass(frozen=True, slots=True)
class ReplyCommitmentInput:
    """一个固定承诺及其强类型载荷。"""

    commitment_type: str
    payload: CommitmentPayload

    def __post_init__(self) -> None:
        if self.commitment_type not in _COMMITMENT_PAYLOAD_TYPES:
            msg = f"不支持的承诺类型: {self.commitment_type!r}"
            raise ValueError(msg)
        expected_type = _COMMITMENT_PAYLOAD_TYPES[self.commitment_type]
        if not isinstance(self.payload, expected_type):
            msg = (
                f"承诺 {self.commitment_type!r} 必须使用 "
                f"{expected_type.__name__}"
            )
            raise TypeError(msg)

    def to_json(self) -> dict[str, Any]:
        return self.payload.to_json()


@dataclass(frozen=True, slots=True)
class ReplyFulfillmentDraft:
    """回复履约首次准备时冻结的父记录与固定子项。"""

    fulfillment_id: str
    payload_hash: str
    request_trace_id: str
    trigger_message_id: str
    trigger_user_id: str
    group_id: str
    bot_self_id: str
    adapter_name: str
    reply_target_message_id: str
    reply_content: str
    commitments: tuple[ReplyCommitmentInput, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "fulfillment_id",
            "payload_hash",
            "request_trace_id",
            "trigger_message_id",
            "trigger_user_id",
            "group_id",
            "bot_self_id",
            "adapter_name",
            "reply_target_message_id",
            "reply_content",
        ):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.commitments, tuple):
            msg = "commitments 必须是固定承诺元组"
            raise TypeError(msg)
        if any(not isinstance(item, ReplyCommitmentInput) for item in self.commitments):
            msg = "commitments 只能包含 ReplyCommitmentInput"
            raise TypeError(msg)
        commitment_types = tuple(item.commitment_type for item in self.commitments)
        if len(commitment_types) != len(COMMITMENT_TYPES) or set(commitment_types) != set(
            COMMITMENT_TYPES
        ):
            msg = "commitments 必须完整包含四种固定承诺且不能重复"
            raise ValueError(msg)


class ReplyFulfillmentRepository:
    """回复履约父子记录的 PostgreSQL adapter。"""

    def __init__(self, pg_pool: Any) -> None:
        self.pg_pool = pg_pool

    async def prepare(self, draft: ReplyFulfillmentDraft) -> bool:
        """在一个事务中插入一个父记录及其四个固定子项。"""
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
                              FROM
                                  komari_chat_reply_fulfillment_commitments AS child
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
    "ReplyFulfillmentDraft",
    "ReplyFulfillmentRepository",
]
