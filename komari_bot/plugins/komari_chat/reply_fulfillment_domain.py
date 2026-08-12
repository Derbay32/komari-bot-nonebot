"""回复履约冻结领域值对象与纯构建函数。

首次持久准备时形成的不可变履约身份、回复正文、Bot 与适配器身份、
发送目标、明确引用目标、适用承诺集合及每项承诺输入，都以本模块的
冻结值对象表达。父子存储 adapter 与旧宽表 adapter 复用本模块的领域
形状，不各自定义载荷协议。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

COMMITMENT_TYPES: tuple[str, ...] = (
    "proactive_reply_confirmation",
    "favorability_adjustment",
    "assistant_reply_history",
    "interaction_history",
)


class ReplyFulfillmentConflictError(ValueError):
    """同一履约身份再次携带不同冻结责任。"""


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        msg = f"{field_name} 必须是字符串"
        raise TypeError(msg)
    if not value:
        msg = f"{field_name} 不能为空"
        raise ValueError(msg)
    return value


def _require_sha256(value: object, field_name: str) -> str:
    text = _require_text(value, field_name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        msg = f"{field_name} 必须是 64 位小写 SHA-256"
        raise ValueError(msg)
    return text


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
        object.__setattr__(
            self,
            "reply_timestamp",
            _require_finite_float(self.reply_timestamp, "reply_timestamp"),
        )

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
    display_name: str
    trigger_size: int
    reply_timestamp: float
    trigger_message_id: str
    record: Mapping[str, str]

    def __post_init__(self) -> None:
        _require_text(self.user_id, "user_id")
        _require_text(self.display_name, "display_name")
        _require_int(self.trigger_size, "trigger_size", minimum=1)
        object.__setattr__(
            self,
            "reply_timestamp",
            _require_finite_float(self.reply_timestamp, "reply_timestamp"),
        )
        _require_text(self.trigger_message_id, "trigger_message_id")
        if not isinstance(self.record, Mapping):
            msg = "record 必须是字符串键值映射"
            raise TypeError(msg)
        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.record.items()
        ):
            msg = "record 必须是字符串键值映射"
            raise TypeError(msg)
        object.__setattr__(self, "record", MappingProxyType(dict(self.record)))

    def to_json(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "display_name": self.display_name,
            "trigger_size": self.trigger_size,
            "reply_timestamp": self.reply_timestamp,
            "trigger_message_id": self.trigger_message_id,
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
    """回复履约首次准备时冻结的父记录与固定子项。

    commitments 只允许 COMMITMENT_TYPES 中按固定顺序排列的非空适用子集，
    不含动态承诺注册。
    """

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
        _require_sha256(self.payload_hash, "payload_hash")
        if not isinstance(self.commitments, tuple):
            msg = "commitments 必须是固定承诺元组"
            raise TypeError(msg)
        if any(not isinstance(item, ReplyCommitmentInput) for item in self.commitments):
            msg = "commitments 只能包含 ReplyCommitmentInput"
            raise TypeError(msg)
        commitment_types = tuple(item.commitment_type for item in self.commitments)
        required_types = {
            "favorability_adjustment",
            "assistant_reply_history",
        }
        if not required_types.issubset(commitment_types):
            msg = "commitments 必须包含好感度调整与角色回复历史"
            raise ValueError(msg)
        if len(commitment_types) != len(set(commitment_types)):
            msg = "commitments 不能包含重复承诺类型"
            raise ValueError(msg)
        if any(
            commitment_type not in COMMITMENT_TYPES
            for commitment_type in commitment_types
        ):
            msg = f"commitments 包含不支持的承诺类型: {commitment_types!r}"
            raise ValueError(msg)
        fixed_order = tuple(
            commitment_type
            for commitment_type in COMMITMENT_TYPES
            if commitment_type in commitment_types
        )
        if commitment_types != fixed_order:
            msg = "commitments 必须按固定顺序排列"
            raise ValueError(msg)


def derive_reply_fulfillment_status(row: Mapping[str, Any]) -> str:
    """按父子表字段推导管理派生状态（与存储 adapter 的 SQL 分支一致）。

    固定状态集合：not_started / pending_confirmation / processing /
    needs_disposition / completed / not_delivered。``row`` 至少携带
    ``delivery_state``、``completed_at`` 与 ``commitments``（子项
    状态事实），供管理投影复用，不读取任何载荷。
    """
    delivery_state = str(row.get("delivery_state") or "")
    if delivery_state == "NOT_STARTED":
        return "not_started"
    if delivery_state == "PENDING_CONFIRMATION":
        return "pending_confirmation"
    if delivery_state == "NOT_DELIVERED":
        return "not_delivered"
    if row.get("completed_at") is not None:
        return "completed"
    commitments = row.get("commitments") or ()
    if any(
        isinstance(item, Mapping) and item.get("state") == "FAILED"
        for item in commitments
    ):
        return "needs_disposition"
    return "processing"


def build_reply_fulfillment_id(
    *,
    group_id: str,
    trigger_message_id: str,
    trigger_user_id: str,
) -> str:
    """由触发消息所在群、触发消息 ID 与触发用户稳定生成履约 ID。

    同一群内同一触发消息至多一个履约；版本化前缀 ``v1`` 保证未来
    调整身份语义时不会与历史记录碰撞。
    """
    _require_text(group_id, "group_id")
    _require_text(trigger_message_id, "trigger_message_id")
    _require_text(trigger_user_id, "trigger_user_id")
    source = f"v1\0{group_id}\0{trigger_message_id}\0{trigger_user_id}"
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return f"reply-{digest}"


def build_reply_fulfillment_payload_hash(
    *,
    fulfillment_id: str,
    trigger_message_id: str,
    trigger_user_id: str,
    group_id: str,
    bot_self_id: str,
    adapter_name: str,
    reply_target_message_id: str,
    reply_content: str,
    commitments: tuple[ReplyCommitmentInput, ...],
) -> str:
    """对全部冻结责任做规范化 SHA-256，JSON 键顺序不影响结果。"""
    payload = {
        "fulfillment_id": fulfillment_id,
        "trigger_message_id": trigger_message_id,
        "trigger_user_id": trigger_user_id,
        "group_id": group_id,
        "bot_self_id": bot_self_id,
        "adapter_name": adapter_name,
        "reply_target_message_id": reply_target_message_id,
        "reply_content": reply_content,
        "commitments": [
            {"commitment_type": item.commitment_type, "payload": item.to_json()}
            for item in commitments
        ],
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
    "build_reply_fulfillment_id",
    "build_reply_fulfillment_payload_hash",
    "derive_reply_fulfillment_status",
]
