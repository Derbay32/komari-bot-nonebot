"""Jev 判定的纯请求、结果与诊断契约；不依赖框架、存储或 SDK。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

type DecisionStatus = Literal[
    "success", "filtered", "bypass", "failed", "cancelled", "duplicate"
]
type ReplyEligibility = Literal["required", "optional", "silent"]
type RetentionPurpose = Literal["candidate", "noise"]
type BusinessRoute = Literal["none", "chat", "summary"]
type HandoffRoute = Literal["chat", "summary", "failure_notification"]
type DecisionReason = Literal[
    "classified",
    "summary_matched",
    "optional_selected",
    "optional_skipped",
    "silent",
    "filtered",
    "platform_bypass",
    "duplicate_event",
    "queue_full",
    "queue_timeout",
    "request_timeout",
    "network_error",
    "rate_limited",
    "authentication_error",
    "provider_error",
    "protocol_error",
    "input_unavailable",
    "disabled",  # 仅历史判定行：当前版本不再产生新的停用结果
    "admission_revoked",
    "interrupted",
    "configuration_invalid",
    "prompt_unavailable",
    "storage_unavailable",
    "initializing",
    "ready",
    "internal_error",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class EventIdentity:
    """平台事件的完整身份；相同正文不等于同一事件。"""

    protocol: str
    account_id: str
    group_id: str
    message_id: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (
                self.protocol,
                self.account_id,
                self.group_id,
                self.message_id,
            )
        ):
            msg = "事件身份的四个部分必须是非空字符串"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class DecisionMessage:
    """文本与已知媒体事实；不接受原始图片载荷。"""

    message_id: str
    group_id: str
    sender_id: str
    text: str
    timestamp: datetime
    display_name: str = ""
    is_bot: bool = False
    reference_ids: tuple[str, ...] = ()
    mention_ids: tuple[str, ...] = ()
    image_present: bool = False
    image_description: str | None = None

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (self.message_id, self.group_id, self.sender_id)
        ):
            msg = "消息身份必须是非空字符串"
            raise ValueError(msg)
        if self.timestamp.utcoffset() is None:
            msg = "消息时间必须包含时区"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class DecisionRequest:
    """一次分发的入口事实；内部复用保持 dispatch_id，外部重投生成新值。"""

    event: EventIdentity
    target: DecisionMessage
    received_at: datetime
    dispatch_id: str = field(default_factory=lambda: str(uuid4()))
    known_references: tuple[DecisionMessage, ...] = ()
    real_platform_at: bool = False
    reply_to_bot: bool = False
    summary_candidate: bool = False
    functional_command: bool = False

    def __post_init__(self) -> None:
        if (
            self.target.group_id != self.event.group_id
            or self.target.message_id != self.event.message_id
        ):
            msg = "当前目标与事件身份不一致"
            raise ValueError(msg)
        if self.received_at.utcoffset() is None:
            msg = "输入截止时间必须包含时区"
            raise ValueError(msg)

    @property
    def platform_summoned(self) -> bool:
        """文本昵称或适配器 to_me 不参与确定性召唤判断。"""
        return self.real_platform_at or self.reply_to_bot


@dataclass(frozen=True, slots=True, kw_only=True)
class ReplyChoice:
    value: ReplyEligibility
    confidence: float
    probabilities: tuple[tuple[ReplyEligibility, float], ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class RetentionChoice:
    value: RetentionPurpose
    confidence: float
    probabilities: tuple[tuple[RetentionPurpose, float], ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class ReferenceSummary:
    complete: bool = True
    resolved_count: int = 0
    missing_count: int = 0
    cycle_count: int = 0
    timed_out: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class DecisionUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PhaseDurations:
    queue_ms: float | None = None
    references_ms: float | None = None
    request_ms: float | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DecisionResult:
    """分类事实与交接路由；外部效果仍须成功领取单次资格。"""

    decision_id: str
    status: DecisionStatus
    reason_code: DecisionReason
    route: BusinessRoute = "none"
    force_reply: bool = False
    reply_choice: ReplyChoice | None = None
    retention_choice: RetentionChoice | None = None
    summary_probability: float | None = None
    effective_reply: ReplyEligibility | None = None
    retain_recent: bool | None = None
    memory_candidate: bool | None = None
    optional_selected: bool | None = None
    references: ReferenceSummary = field(default_factory=ReferenceSummary)
    durations: PhaseDurations = field(default_factory=PhaseDurations)
    usage: DecisionUsage = field(default_factory=DecisionUsage)
    config_revision: int | None = None
    prompt_revision: int | None = None
    requested_model: str | None = None
    actual_model: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DecisionHealthSnapshot:
    """被动健康快照；没有观测证据的字段保持未知。"""

    runtime_status: Literal["ready", "failed"] = "failed"
    reason_code: DecisionReason = "initializing"
    last_request_status: Literal["unknown", "success", "failure"] = "unknown"
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_failure_reason_code: DecisionReason | None = None
    consecutive_failures: int = 0
    in_flight_count: int | None = None
    queued_count: int | None = None
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True, kw_only=True)
class DecisionLogMetadata:
    """元数据没有正文、昵称、Prompt、错误字符串或预览字段。"""

    decision_id: str
    event: EventIdentity
    created_at: datetime
    finished_at: datetime
    result: DecisionResult
