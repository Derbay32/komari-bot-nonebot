"""判定引擎运行时三态降级测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from komari_bot.plugins.komari_decision.services import decision_engine as engine_module
from komari_bot.plugins.komari_decision.services.decision_engine import DecisionEngine
from komari_bot.plugins.komari_decision.services.runtime_state import (
    DecisionRuntimeState,
    DecisionRuntimeStatus,
)
from komari_bot.plugins.komari_decision.services.unified_candidate_rerank import (
    SceneRuntimeUnavailableError,
    UnifiedRerankResult,
)

if TYPE_CHECKING:
    from collections.abc import Callable


class _RerankService:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    async def rank_message(self, _message: str) -> UnifiedRerankResult:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return UnifiedRerankResult(
            alias_hit=False,
            candidates=[],
            score_map={},
            meaningful_score=0.9,
            noise_score=0.1,
            call_direct_score=None,
            call_mention_score=None,
            best_scene_id="SCENE_TEST",
            best_scene_score=0.9,
            meaningful_prior=0.8,
            noise_prior=0.2,
        )


class _SocialTimingService:
    async def score(self, **_kwargs: object) -> Any:
        return SimpleNamespace(timing_score=0.5)


def _patch_decision_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    filter_calls: list[str] = []

    async def _preprocess_message(**kwargs: object) -> Any:
        filter_calls.append(str(kwargs["message"]))
        return SimpleNamespace(should_skip=False, reason=None)

    monkeypatch.setattr(engine_module, "preprocess_message", _preprocess_message)
    monkeypatch.setattr(
        engine_module,
        "get_config",
        lambda: SimpleNamespace(
            timing_weight=0.3,
            reply_threshold=0.5,
            noise_conf_threshold=0.76,
            noise_margin_threshold=0.1,
            call_margin_threshold=0.08,
        ),
    )
    return filter_calls


def _build_engine(
    monkeypatch: pytest.MonkeyPatch,
    state_provider: Callable[[], DecisionRuntimeState],
    rerank: _RerankService,
) -> DecisionEngine:
    engine = DecisionEngine(
        cast("Any", object()),
        runtime_state_provider=state_provider,
    )
    monkeypatch.setattr(engine, "_unified_rerank", rerank)
    monkeypatch.setattr(engine, "_social_timing", _SocialTimingService())
    return engine


@pytest.mark.asyncio
async def test_ready_runtime_executes_proactive_rerank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filter_calls = _patch_decision_dependencies(monkeypatch)
    rerank = _RerankService()
    engine = _build_engine(monkeypatch, DecisionRuntimeState.ready, rerank)

    outcome = await engine.evaluate(
        message_content="这是一条有意义的群聊消息",
        group_id="100",
        at_trigger=False,
    )

    assert filter_calls == ["这是一条有意义的群聊消息"]
    assert rerank.calls == 1
    assert outcome.should_reply is True
    assert outcome.runtime_status is DecisionRuntimeStatus.READY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        DecisionRuntimeState.disabled("测试关闭"),
        DecisionRuntimeState.failed("测试初始化失败"),
    ],
)
async def test_unavailable_runtime_skips_proactive_rerank(
    monkeypatch: pytest.MonkeyPatch,
    state: DecisionRuntimeState,
) -> None:
    filter_calls = _patch_decision_dependencies(monkeypatch)
    rerank = _RerankService()
    engine = _build_engine(monkeypatch, lambda: state, rerank)

    outcome = await engine.evaluate(
        message_content="不会触发主动回复",
        group_id="100",
        at_trigger=False,
    )

    assert filter_calls == ["不会触发主动回复"]
    assert rerank.calls == 0
    assert outcome.memory_action == "store"
    assert outcome.should_reply is False
    assert outcome.runtime_status is state.status


@pytest.mark.asyncio
async def test_explicit_trigger_bypasses_failed_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filter_calls = _patch_decision_dependencies(monkeypatch)
    rerank = _RerankService()
    engine = _build_engine(
        monkeypatch,
        lambda: DecisionRuntimeState.failed("测试初始化失败"),
        rerank,
    )

    outcome = await engine.evaluate(
        message_content="@小鞠 你好",
        group_id="100",
        at_trigger=True,
    )

    assert filter_calls == []
    assert rerank.calls == 0
    assert outcome.should_reply is True
    assert outcome.force_reply is True
    assert outcome.runtime_status is DecisionRuntimeStatus.FAILED


@pytest.mark.asyncio
async def test_transient_snapshot_loss_degrades_without_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_decision_dependencies(monkeypatch)
    rerank = _RerankService(SceneRuntimeUnavailableError("快照暂不可用"))
    engine = _build_engine(monkeypatch, DecisionRuntimeState.ready, rerank)

    outcome = await engine.evaluate(
        message_content="运行中快照刚好失效",
        group_id="100",
        at_trigger=False,
    )

    assert rerank.calls == 1
    assert outcome.should_reply is False
    assert outcome.runtime_status is DecisionRuntimeStatus.FAILED
    assert outcome.runtime_reason == "快照暂不可用"
