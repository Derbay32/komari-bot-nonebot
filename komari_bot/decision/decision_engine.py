"""判定输出契约：DecisionOutcome 与 Literal 别名。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .runtime_state import DecisionRuntimeStatus
    from .social_timing_service import TimingScoreBreakdown
    from .unified_candidate_rerank import UnifiedRerankResult

CallIntent = Literal["none", "ambiguous", "direct_call", "casual_mention"]
MemoryAction = Literal["store", "drop"]
ReplyReason = Literal["at", "direct_call", "score", "none"]


@dataclass(frozen=True)
class DecisionOutcome:
    """判定输出。"""

    memory_action: MemoryAction
    should_reply: bool
    force_reply: bool
    reply_reason: ReplyReason
    forced_reply_reason: Literal["at", "direct_call", "none"]
    reply_score: float | None
    alias_hit: bool | None
    call_intent: CallIntent
    call_margin: float | None
    best_scene_id: str | None
    scene_score: float | None
    timing_score: float | None
    noise_score: float | None
    meaningful_score: float | None
    call_direct_score: float | None
    call_mention_score: float | None
    filter_reason: Literal["short", "history_repeat", "none", "command"] | None
    rank_result: UnifiedRerankResult | None
    timing_breakdown: TimingScoreBreakdown | None
    runtime_status: DecisionRuntimeStatus
    runtime_reason: str


__all__ = ["CallIntent", "DecisionOutcome", "MemoryAction", "ReplyReason"]
