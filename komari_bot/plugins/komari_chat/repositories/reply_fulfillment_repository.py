"""回复履约父子存储 adapter。

本模块只提供 TSK-79 的持久化边界，正常聊天路径仍由旧版回复提交
仓库负责。承诺载荷在进入 JSONB 前必须经过本模块的固定 dataclass
校验与序列化。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg


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

    def __init__(self, pg_pool: asyncpg.Pool[Any]) -> None:
        self.pg_pool = pg_pool


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
