"""聊天用途迁入深场景重排 module 的验收测试（KOMARIBOT-26 / KOMARIBOT-27）。"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any, cast

import pytest

from komari_bot import decision as decision_contracts
from komari_bot.plugins.komari_decision import __all__ as decision_plugin_all
from komari_bot.plugins.komari_decision.services import (
    __all__ as decision_services_all,
)
from komari_bot.plugins.komari_decision.services import (
    decision_engine,
    scene_classification,
)
from komari_bot.plugins.komari_decision.services.scene_classification import (
    ChatSceneUnavailableError,
)
from komari_bot.plugins.komari_decision.services.scene_runtime_service import (
    SceneRuntimeGeneralCandidate,
    SceneRuntimeSnapshot,
)


class _Runtime:
    def __init__(
        self,
        snapshot: SceneRuntimeSnapshot | None,
        *,
        refresh_error: Exception | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.refresh_error = refresh_error
        self.refresh_calls = 0

    async def refresh_if_runtime_updated(self) -> bool:
        self.refresh_calls += 1
        if self.refresh_error is not None:
            raise self.refresh_error
        return False

    def get_scene_candidates(self) -> SceneRuntimeSnapshot | None:
        return self.snapshot


class _Provider:
    def __init__(self) -> None:
        self.embed_calls: list[tuple[str, str]] = []
        self.rerank_calls: list[dict[str, object]] = []

    def is_rerank_enabled(self) -> bool:
        msg = "聊天用途不得检查 rerank 开关或切换到总结余弦 fallback"
        raise AssertionError(msg)

    async def embed(self, text: str, instruction: str = "") -> list[float]:
        self.embed_calls.append((text, instruction))
        return [1.0, 0.0]

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int | None = None,
        instruction: str = "",
    ) -> list[SimpleNamespace]:
        self.rerank_calls.append(
            {
                "query": query,
                "documents": documents,
                "top_n": top_n,
                "instruction": instruction,
            }
        )
        scores = [0.1, 0.8, 0.9, 0.2, 0.7, 0.6]
        return [
            SimpleNamespace(index=index, relevance_score=score)
            for index, score in enumerate(scores[: len(documents)])
        ]


def _snapshot() -> SceneRuntimeSnapshot:
    return SceneRuntimeSnapshot(
        set_id=7,
        runtime_updated_at="2026-08-10T00:00:00Z",
        fixed_candidates={
            "NOISE": "无意义噪声",
            "MEANINGFUL": "有意义消息",
            "CALL_DIRECT": "直接呼叫角色",
            "CALL_MENTION": "提及角色",
        },
        fixed_embeddings={
            "NOISE": [0.0, 1.0],
            "MEANINGFUL": [1.0, 0.0],
            "CALL_DIRECT": [1.0, 0.0],
            "CALL_MENTION": [0.0, 1.0],
        },
        general_candidates=[
            SceneRuntimeGeneralCandidate(
                scene_id="scene_high",
                text="高相似场景",
                embedding=[1.0, 0.0],
                order_index=10,
            ),
            SceneRuntimeGeneralCandidate(
                scene_id="scene_low",
                text="低相似场景",
                embedding=[0.0, 1.0],
                order_index=20,
            ),
            SceneRuntimeGeneralCandidate(
                scene_id="scene_middle",
                text="中相似场景",
                embedding=[0.5, 0.5],
                order_index=30,
            ),
        ],
    )


def _install_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    provider: _Provider,
) -> None:
    monkeypatch.setattr(
        scene_classification,
        "_get_embedding_provider",
        lambda: provider,
    )
    monkeypatch.setattr(
        scene_classification,
        "get_config",
        lambda: SimpleNamespace(
            bot_aliases=["小鞠"],
            scene_top_k=2,
            embedding_instruction_query="聊天查询指令",
            rerank_instruction="聊天精排指令",
        ),
        raising=False,
    )


def test_decision_engine_uses_internal_deep_chat_operation() -> None:
    """DecisionEngine 不再构造旧宽 service，深 operation 也不暴露 purpose 参数。"""
    engine_source = inspect.getsource(decision_engine)
    assert "UnifiedCandidateRerankService" not in engine_source
    assert "_unified_rerank" not in engine_source
    rank_chat_message = cast("Any", scene_classification).rank_chat_message
    assert cast("Any", decision_engine).rank_chat_message is rank_chat_message
    assert "purpose" not in inspect.signature(
        rank_chat_message
    ).parameters
    assert "rank_chat_message" not in scene_classification.__all__


@pytest.mark.asyncio
async def test_chat_operation_preserves_candidates_alias_instructions_and_scores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _Provider()
    _install_dependencies(monkeypatch, provider)
    runtime = _Runtime(_snapshot())
    monkeypatch.setattr(
        scene_classification,
        "_get_rerank_failure_budget",
        lambda: pytest.fail("聊天用途不得读取群总结失败预算"),
    )

    result = await cast("Any", scene_classification).rank_chat_message(
        "小鞠，看看这条消息",
        scene_runtime=runtime,  # type: ignore[arg-type]
    )

    assert runtime.refresh_calls == 1
    assert provider.embed_calls == [("小鞠，看看这条消息", "聊天查询指令")]
    assert [candidate.key for candidate in result.candidates] == [
        "NOISE",
        "MEANINGFUL",
        "CALL_DIRECT",
        "CALL_MENTION",
        "SCENE::scene_high",
        "SCENE::scene_middle",
    ]
    assert provider.rerank_calls == [
        {
            "query": "小鞠，看看这条消息",
            "documents": [
                "无意义噪声",
                "有意义消息",
                "直接呼叫角色",
                "提及角色",
                "高相似场景",
                "中相似场景",
            ],
            "top_n": 6,
            "instruction": "聊天精排指令",
        }
    ]
    assert result.alias_hit is True
    assert result.noise_score == 0.1
    assert result.meaningful_score == 0.8
    assert result.call_direct_score == 0.9
    assert result.call_mention_score == 0.2
    assert result.best_scene_id == "scene_high"
    assert result.best_scene_score == 0.7
    assert result.noise_prior == 0.0
    assert result.meaningful_prior == 1.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runtime",
    [
        _Runtime(None),
        _Runtime(_snapshot(), refresh_error=RuntimeError("刷新失败")),
    ],
)
async def test_chat_operation_keeps_runtime_unavailable_error_contract(
    monkeypatch: pytest.MonkeyPatch,
    runtime: _Runtime,
) -> None:
    _install_dependencies(monkeypatch, _Provider())

    with pytest.raises(ChatSceneUnavailableError):
        await cast("Any", scene_classification).rank_chat_message(
            "普通聊天",
            scene_runtime=runtime,  # type: ignore[arg-type]
        )


def test_chat_operation_type_annotations_do_not_need_public_purpose() -> None:
    hints: dict[str, Any] = inspect.get_annotations(
        cast("Any", scene_classification).rank_chat_message
    )
    assert "purpose" not in hints


def test_chat_only_types_stay_inside_deep_implementation() -> None:
    """聊天专用候选/结果/异常只存在于深 implementation，不泄漏到任何公开面。

    KOMARIBOT-27 起聊天宽重排契约退役：共享包、插件顶层与 services 包均
    不得再导出聊天专用类型（旧名与新名都不允许）。
    """
    chat_only_types = {
        "ChatCandidate",
        "ChatRerankResult",
        "ChatSceneUnavailableError",
    }
    public_all_lists = (
        decision_contracts.__all__,
        decision_plugin_all,
        decision_services_all,
        scene_classification.__all__,
    )
    for exported in public_all_lists:
        assert chat_only_types.isdisjoint(exported)
    assert not any(
        hasattr(decision_contracts, name) for name in chat_only_types
    )
    assert "rank_chat_message" not in scene_classification.__all__
