"""回复/记忆统一决策引擎。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from nonebot import logger

from .config_interface import get_config
from .message_filter import preprocess_message
from .runtime_state import DecisionRuntimeState, DecisionRuntimeStatus
from .social_timing_service import SocialTimingService, TimingScoreBreakdown
from .unified_candidate_rerank import (
    SceneRuntimeUnavailableError,
    UnifiedCandidateRerankService,
    UnifiedRerankResult,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from komari_bot.plugins.komari_decision.services.scene_runtime_service import (
        SceneRuntimeService,
    )
    from komari_bot.plugins.komari_memory.services.redis_manager import RedisManager

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


class DecisionEngine:
    """对单条消息给出 reply/memory 动作。"""

    _DIRECT_CALL_BONUS = 0.25
    _CASUAL_MENTION_PENALTY = -0.18

    def __init__(
        self,
        redis: RedisManager,
        scene_runtime: SceneRuntimeService | None = None,
        runtime_state_provider: Callable[[], DecisionRuntimeState] | None = None,
    ) -> None:
        self._redis = redis
        self._scene_runtime = scene_runtime
        self._runtime_state_provider = runtime_state_provider
        self._unified_rerank = UnifiedCandidateRerankService(runtime_service=scene_runtime)
        self._social_timing = SocialTimingService(redis)

    def _get_runtime_state(self) -> DecisionRuntimeState:
        """读取运行时状态；状态提供器异常时按 failed 降级。"""
        if self._runtime_state_provider is not None:
            try:
                return self._runtime_state_provider()
            except Exception as exc:
                logger.exception("[KomariDecision] 读取运行时状态失败")
                return DecisionRuntimeState.failed(
                    f"运行时状态读取失败：{type(exc).__name__}"
                )

        if self._scene_runtime is None:
            return DecisionRuntimeState.disabled("scene runtime 未配置")
        if self._scene_runtime.get_scene_candidates() is None:
            return DecisionRuntimeState.failed("scene runtime snapshot 暂不可用")
        return DecisionRuntimeState.ready()

    @staticmethod
    def _build_degraded_outcome(
        runtime_state: DecisionRuntimeState,
    ) -> DecisionOutcome:
        """scene 不可用时保留消息，但禁止主动回复。"""
        return DecisionOutcome(
            memory_action="store",
            should_reply=False,
            force_reply=False,
            reply_reason="none",
            forced_reply_reason="none",
            reply_score=None,
            alias_hit=None,
            call_intent="none",
            call_margin=None,
            best_scene_id=None,
            scene_score=None,
            timing_score=None,
            noise_score=None,
            meaningful_score=None,
            call_direct_score=None,
            call_mention_score=None,
            filter_reason=None,
            rank_result=None,
            timing_breakdown=None,
            runtime_status=runtime_state.status,
            runtime_reason=runtime_state.reason,
        )

    async def evaluate(
        self,
        *,
        message_content: str,
        group_id: str,
        at_trigger: bool,
    ) -> DecisionOutcome:
        """执行完整判定流程。"""
        config = get_config()
        runtime_state = self._get_runtime_state()

        if at_trigger:
            return DecisionOutcome(
                memory_action="store",
                should_reply=True,
                force_reply=True,
                reply_reason="at",
                forced_reply_reason="at",
                reply_score=None,
                alias_hit=None,
                call_intent="none",
                call_margin=None,
                best_scene_id=None,
                scene_score=None,
                timing_score=None,
                noise_score=None,
                meaningful_score=None,
                call_direct_score=None,
                call_mention_score=None,
                filter_reason=None,
                rank_result=None,
                timing_breakdown=None,
                runtime_status=runtime_state.status,
                runtime_reason=runtime_state.reason,
            )

        filter_result = await preprocess_message(
            message=message_content,
            config=config,
            redis=self._redis,
            group_id=group_id,
        )
        if filter_result.should_skip:
            return DecisionOutcome(
                memory_action="drop",
                should_reply=False,
                force_reply=False,
                reply_reason="none",
                forced_reply_reason="none",
                reply_score=None,
                alias_hit=None,
                call_intent="none",
                call_margin=None,
                best_scene_id=None,
                scene_score=None,
                timing_score=None,
                noise_score=None,
                meaningful_score=None,
                call_direct_score=None,
                call_mention_score=None,
                filter_reason=filter_result.reason,
                rank_result=None,
                timing_breakdown=None,
                runtime_status=runtime_state.status,
                runtime_reason=runtime_state.reason,
            )

        if not runtime_state.is_ready:
            logger.debug(
                "[KomariDecision] 主动回复判定降级跳过: status={} reason={}",
                runtime_state.status,
                runtime_state.reason,
            )
            return self._build_degraded_outcome(runtime_state)

        try:
            rank_result = await self._unified_rerank.rank_message(message_content)
        except SceneRuntimeUnavailableError as exc:
            unavailable_state = DecisionRuntimeState.failed(str(exc))
            logger.warning(
                "[KomariDecision] scene runtime 暂不可用，跳过主动回复判定: {}",
                exc,
            )
            return self._build_degraded_outcome(unavailable_state)
        memory_action: MemoryAction = (
            "drop" if self._should_drop_memory(rank_result) else "store"
        )
        call_intent, call_margin = self._resolve_call_intent(rank_result)

        timing_result = await self._social_timing.score(
            group_id=group_id,
            alias_hit=rank_result.alias_hit,
        )
        scene_score = rank_result.best_scene_score
        alias_adjust = self._get_alias_adjust(call_intent)
        reply_score = self._clamp_score(
            (1.0 - config.timing_weight) * scene_score
            + config.timing_weight * timing_result.timing_score
            + alias_adjust
        )

        if call_intent == "direct_call":
            return DecisionOutcome(
                memory_action=memory_action,
                should_reply=True,
                force_reply=True,
                reply_reason="direct_call",
                forced_reply_reason="direct_call",
                reply_score=reply_score,
                alias_hit=rank_result.alias_hit,
                call_intent=call_intent,
                call_margin=call_margin,
                best_scene_id=rank_result.best_scene_id,
                scene_score=scene_score,
                timing_score=timing_result.timing_score,
                noise_score=rank_result.noise_score,
                meaningful_score=rank_result.meaningful_score,
                call_direct_score=rank_result.call_direct_score,
                call_mention_score=rank_result.call_mention_score,
                filter_reason=None,
                rank_result=rank_result,
                timing_breakdown=timing_result,
                runtime_status=runtime_state.status,
                runtime_reason=runtime_state.reason,
            )

        should_reply = reply_score >= config.reply_threshold
        return DecisionOutcome(
            memory_action=memory_action,
            should_reply=should_reply,
            force_reply=False,
            reply_reason="score" if should_reply else "none",
            forced_reply_reason="none",
            reply_score=reply_score,
            alias_hit=rank_result.alias_hit,
            call_intent=call_intent,
            call_margin=call_margin,
            best_scene_id=rank_result.best_scene_id,
            scene_score=scene_score,
            timing_score=timing_result.timing_score,
            noise_score=rank_result.noise_score,
            meaningful_score=rank_result.meaningful_score,
            call_direct_score=rank_result.call_direct_score,
            call_mention_score=rank_result.call_mention_score,
            filter_reason=None,
            rank_result=rank_result,
            timing_breakdown=timing_result,
            runtime_status=runtime_state.status,
            runtime_reason=runtime_state.reason,
        )

    @staticmethod
    def _clamp_score(value: float) -> float:
        return max(0.0, min(1.0, value))

    @staticmethod
    def _get_alias_adjust(intent: CallIntent) -> float:
        if intent == "direct_call":
            return DecisionEngine._DIRECT_CALL_BONUS
        if intent == "casual_mention":
            return DecisionEngine._CASUAL_MENTION_PENALTY
        return 0.0

    @staticmethod
    def _resolve_call_intent(
        rank_result: UnifiedRerankResult,
    ) -> tuple[CallIntent, float]:
        config = get_config()

        if (
            not rank_result.alias_hit
            or rank_result.call_direct_score is None
            or rank_result.call_mention_score is None
        ):
            return "none", 0.0

        call_margin = rank_result.call_direct_score - rank_result.call_mention_score
        threshold = config.call_margin_threshold
        if call_margin >= threshold:
            return "direct_call", call_margin
        if call_margin <= -threshold:
            return "casual_mention", call_margin
        return "ambiguous", call_margin

    @staticmethod
    def _should_drop_memory(rank_result: UnifiedRerankResult) -> bool:
        config = get_config()
        noise_delta = rank_result.noise_score - rank_result.meaningful_score
        return (
            rank_result.noise_score >= config.noise_conf_threshold
            and noise_delta >= config.noise_margin_threshold
        )
