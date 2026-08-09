"""群总结请求场景归类顶层 operation 验收测试（KOMARIBOT-23）。"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import komari_bot.plugins.komari_decision as decision_plugin
from komari_bot.decision import (
    DecisionRuntimeState,
    SummaryRequestClassificationResult,
    SummaryRequestClassificationStatus,
    SummaryRequestUnavailableReason,
)
from komari_bot.plugins.embedding_provider import (
    EmbeddingResponseValidationError,
    RemoteServiceRequestError,
    RerankResponseValidationError,
)
from komari_bot.plugins.komari_decision.config_schema import (
    KomariDecisionConfigSchema,
)
from komari_bot.plugins.komari_decision.services import scene_classification
from komari_bot.plugins.komari_decision.services.scene_runtime_service import (
    SceneRuntimeGeneralCandidate,
    SceneRuntimeSnapshot,
)

SUMMARY_SCENE = "scene_group_history_summary"


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


class _EmbeddingProvider:
    def __init__(
        self,
        *,
        query_vector: list[float] | None = None,
        rerank_scores: list[float] | None = None,
        embedding_ready: bool = True,
        rerank_enabled: bool = True,
        embed_error: BaseException | None = None,
        rerank_error: BaseException | None = None,
    ) -> None:
        self.query_vector = query_vector or [1.0, 0.0]
        self.rerank_scores = rerank_scores or []
        self.embedding_ready = embedding_ready
        self.rerank_enabled = rerank_enabled
        self.embed_error = embed_error
        self.rerank_error = rerank_error
        self.embed_calls: list[tuple[str, str]] = []
        self.rerank_calls: list[dict[str, object]] = []

    def is_rerank_enabled(self) -> bool:
        return self.rerank_enabled

    def is_embedding_ready(self) -> bool:
        return self.embedding_ready

    async def embed(self, text: str, instruction: str = "") -> list[float]:
        self.embed_calls.append((text, instruction))
        if self.embed_error is not None:
            raise self.embed_error
        return self.query_vector

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
        if self.rerank_error is not None:
            raise self.rerank_error
        return [
            SimpleNamespace(index=index, relevance_score=score)
            for index, score in enumerate(self.rerank_scores)
        ]


def _snapshot(
    *,
    include_summary: bool = True,
) -> SceneRuntimeSnapshot:
    candidates = [
        SceneRuntimeGeneralCandidate(
            scene_id="scene_casual_chat",
            text="普通聊天和观点交流",
            embedding=[0.0, 1.0],
            order_index=10,
        )
    ]
    if include_summary:
        candidates.insert(
            0,
            SceneRuntimeGeneralCandidate(
                scene_id=SUMMARY_SCENE,
                text="请求总结群聊历史内容",
                embedding=[1.0, 0.0],
                order_index=5,
            ),
        )
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
            "CALL_DIRECT": [0.0, 1.0],
            "CALL_MENTION": [0.0, 1.0],
        },
        general_candidates=candidates,
    )


def _config(**overrides: object) -> KomariDecisionConfigSchema:
    values: dict[str, object] = {
        "plugin_enable": True,
        "scene_persist_enabled": True,
        "summary_scene_top_k": 4,
        "summary_rerank_enabled": True,
        "summary_rerank_threshold": 0.6,
        "summary_similarity_threshold": 0.7,
    }
    values.update(overrides)
    return KomariDecisionConfigSchema(**values)


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    config: KomariDecisionConfigSchema,
    runtime: _Runtime | None,
    provider: _EmbeddingProvider,
    runtime_state: DecisionRuntimeState | None = None,
) -> None:
    monkeypatch.setattr(decision_plugin, "get_config", lambda: config)
    manager = SimpleNamespace(
        runtime_state=runtime_state or DecisionRuntimeState.ready(),
        scene_runtime=runtime,
    )
    monkeypatch.setattr(decision_plugin, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(
        scene_classification,
        "_get_embedding_provider",
        lambda: provider,
    )


def test_summary_classification_contract_is_narrow_and_nonebot_free() -> None:
    """共享结果只暴露三态与安全原因码，不携带实现诊断。"""
    contract_path = (
        Path(__file__).resolve().parents[2]
        / "komari_bot"
        / "decision"
        / "summary_request_classification.py"
    )
    source = contract_path.read_text(encoding="utf-8")

    assert "nonebot" not in source
    assert {status.value for status in SummaryRequestClassificationStatus} == {
        "matched",
        "not_matched",
        "unavailable",
    }
    assert [field.name for field in fields(SummaryRequestClassificationResult)] == [
        "status",
        "reason",
    ]
    result = SummaryRequestClassificationResult.unavailable(
        SummaryRequestUnavailableReason.RUNTIME_UNAVAILABLE
    )
    assert result.status is SummaryRequestClassificationStatus.UNAVAILABLE
    assert result.reason is SummaryRequestUnavailableReason.RUNTIME_UNAVAILABLE
    for forbidden in ("scene_id", "score", "threshold", "exception", "message"):
        assert not hasattr(result, forbidden)

    with pytest.raises(ValueError, match="不可用结果必须携带原因码"):
        SummaryRequestClassificationResult(
            status=SummaryRequestClassificationStatus.UNAVAILABLE,
            reason=None,
        )
    with pytest.raises(ValueError, match="命中或未命中结果不能携带原因码"):
        SummaryRequestClassificationResult(
            status=SummaryRequestClassificationStatus.MATCHED,
            reason=SummaryRequestUnavailableReason.RUNTIME_UNAVAILABLE,
        )


def test_summary_classification_operation_has_one_narrow_argument() -> None:
    """调用方不能传 runtime、场景键、阈值、候选 flags 或 trace。"""
    signature = inspect.signature(decision_plugin.classify_summary_request)
    assert list(signature.parameters) == ["message_text"]


@pytest.mark.asyncio
async def test_numeric_fast_path_runs_only_after_runtime_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _EmbeddingProvider(embed_error=AssertionError("不应调用 embedding"))
    runtime = _Runtime(_snapshot())
    _wire(
        monkeypatch,
        config=_config(plugin_enable=False),
        runtime=runtime,
        provider=provider,
        runtime_state=DecisionRuntimeState.disabled("配置关闭"),
    )

    disabled = await decision_plugin.classify_summary_request("总结过去 50 条")

    assert disabled == SummaryRequestClassificationResult.unavailable(
        SummaryRequestUnavailableReason.DECISION_DISABLED
    )
    assert provider.embed_calls == []
    assert runtime.refresh_calls == 0

    _wire(
        monkeypatch,
        config=_config(),
        runtime=runtime,
        provider=provider,
        runtime_state=DecisionRuntimeState.ready(),
    )

    matched = await decision_plugin.classify_summary_request("  总结   过去 50 条 ")

    assert matched == SummaryRequestClassificationResult.matched()
    assert provider.embed_calls == []
    assert provider.rerank_calls == []

    broad_numeric = await decision_plugin.classify_summary_request(
        "请总结今天 8 点后的聊天"
    )
    assert broad_numeric == SummaryRequestClassificationResult.matched()
    assert provider.embed_calls == []


@pytest.mark.asyncio
async def test_summary_operation_uses_only_general_scenes_and_frozen_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _config(
        summary_scene_top_k=2,
        summary_embedding_instruction_query="总结查询指令-v1",
        summary_rerank_instruction="总结精排指令-v1",
    )
    second = _config(
        summary_scene_top_k=1,
        summary_embedding_instruction_query="不应读取-v2",
        summary_rerank_instruction="不应读取-v2",
    )
    configs = iter([first, second])
    config_reads = 0

    def _get_config() -> KomariDecisionConfigSchema:
        nonlocal config_reads
        config_reads += 1
        return next(configs)

    provider = _EmbeddingProvider(rerank_scores=[0.9, 0.2])
    runtime = _Runtime(_snapshot())
    manager = decision_plugin.PluginManager()
    manager.scene_runtime = cast("Any", runtime)
    monkeypatch.setattr(decision_plugin, "get_config", _get_config)
    monkeypatch.setattr(
        decision_plugin,
        "get_plugin_manager",
        lambda: manager,
    )
    monkeypatch.setattr(
        scene_classification,
        "_get_embedding_provider",
        lambda: provider,
    )

    result = await decision_plugin.classify_summary_request(
        "  总结一下\n今天聊了什么  "
    )

    assert result == SummaryRequestClassificationResult.matched()
    assert config_reads == 1
    assert provider.embed_calls == [
        ("总结一下今天聊了什么", "总结查询指令-v1")
    ]
    assert provider.rerank_calls == [
        {
            "query": "总结一下今天聊了什么",
            "documents": ["请求总结群聊历史内容", "普通聊天和观点交流"],
            "top_n": 2,
            "instruction": "总结精排指令-v1",
        }
    ]
    all_documents = provider.rerank_calls[0]["documents"]
    assert isinstance(all_documents, list)
    assert not set(all_documents) & set(_snapshot().fixed_candidates.values())


@pytest.mark.asyncio
async def test_non_summary_best_scene_returns_not_matched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _EmbeddingProvider(rerank_scores=[0.2, 0.95])
    _wire(
        monkeypatch,
        config=_config(),
        runtime=_Runtime(_snapshot()),
        provider=provider,
    )

    result = await decision_plugin.classify_summary_request(
        "你觉得这份总结写得怎么样"
    )

    assert result == SummaryRequestClassificationResult.not_matched()


@pytest.mark.asyncio
async def test_explicit_cosine_mode_uses_real_similarity_without_rerank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _EmbeddingProvider(
        query_vector=[1.0, 0.0],
        rerank_scores=[0.0, 1.0],
    )
    _wire(
        monkeypatch,
        config=_config(
            summary_rerank_enabled=False,
            summary_similarity_threshold=0.8,
        ),
        runtime=_Runtime(_snapshot()),
        provider=provider,
    )

    result = await decision_plugin.classify_summary_request(
        "请把今天的群聊做个总结"
    )

    assert result == SummaryRequestClassificationResult.matched()
    assert provider.rerank_calls == []


@pytest.mark.asyncio
async def test_provider_disabled_rerank_uses_real_cosine_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _EmbeddingProvider(
        query_vector=[1.0, 0.0],
        rerank_enabled=False,
        rerank_scores=[0.0, 1.0],
    )
    _wire(
        monkeypatch,
        config=_config(
            summary_rerank_enabled=True,
            summary_similarity_threshold=0.8,
        ),
        runtime=_Runtime(_snapshot()),
        provider=provider,
    )

    result = await decision_plugin.classify_summary_request(
        "请把今天的群聊做个总结"
    )

    assert result == SummaryRequestClassificationResult.matched()
    assert provider.rerank_calls == []


@pytest.mark.asyncio
async def test_uninitialized_embedding_precedes_cosine_configuration_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _EmbeddingProvider(
        embedding_ready=False,
        rerank_enabled=False,
        embed_error=AssertionError("不应调用未就绪的 embedding"),
    )
    _wire(
        monkeypatch,
        config=_config(
            summary_rerank_enabled=True,
            summary_similarity_threshold=None,
        ),
        runtime=_Runtime(_snapshot()),
        provider=provider,
    )

    result = await decision_plugin.classify_summary_request(
        "请把今天的群聊做个总结"
    )

    assert result == SummaryRequestClassificationResult.unavailable(
        SummaryRequestUnavailableReason.EMBEDDING_UNAVAILABLE
    )
    assert provider.embed_calls == []


@pytest.mark.asyncio
async def test_cosine_mode_requires_configured_similarity_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _EmbeddingProvider()
    _wire(
        monkeypatch,
        config=_config(
            summary_rerank_enabled=False,
            summary_similarity_threshold=None,
        ),
        runtime=_Runtime(_snapshot()),
        provider=provider,
    )

    result = await decision_plugin.classify_summary_request("总结一下今天的聊天")

    assert result == SummaryRequestClassificationResult.unavailable(
        SummaryRequestUnavailableReason.CONFIGURATION_INCOMPLETE
    )
    assert provider.embed_calls == []


@pytest.mark.asyncio
async def test_expected_runtime_scene_and_embedding_failures_are_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _EmbeddingProvider()
    cases = [
        (
            _Runtime(_snapshot(), refresh_error=RuntimeError("数据库连接中断")),
            provider,
            SummaryRequestUnavailableReason.RUNTIME_UNAVAILABLE,
        ),
        (
            _Runtime(_snapshot(include_summary=False)),
            provider,
            SummaryRequestUnavailableReason.SCENE_DATA_UNAVAILABLE,
        ),
        (
            _Runtime(_snapshot()),
            _EmbeddingProvider(embed_error=RuntimeError("EmbeddingProvider 尚未初始化")),
            SummaryRequestUnavailableReason.EMBEDDING_UNAVAILABLE,
        ),
        (
            _Runtime(_snapshot()),
            _EmbeddingProvider(embed_error=TimeoutError("embedding 请求超时")),
            SummaryRequestUnavailableReason.EMBEDDING_UNAVAILABLE,
        ),
        (
            _Runtime(_snapshot()),
            _EmbeddingProvider(
                embed_error=RemoteServiceRequestError("embedding_api 请求失败")
            ),
            SummaryRequestUnavailableReason.EMBEDDING_UNAVAILABLE,
        ),
        (
            _Runtime(_snapshot()),
            _EmbeddingProvider(
                embed_error=EmbeddingResponseValidationError("向量维度不匹配")
            ),
            SummaryRequestUnavailableReason.EMBEDDING_UNAVAILABLE,
        ),
    ]

    for runtime, current_provider, reason in cases:
        _wire(
            monkeypatch,
            config=_config(),
            runtime=runtime,
            provider=current_provider,
        )
        result = await decision_plugin.classify_summary_request(
            "总结一下今天聊了什么"
        )
        assert result == SummaryRequestClassificationResult.unavailable(reason)


@pytest.mark.asyncio
async def test_runtime_and_rerank_programming_errors_propagate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _Runtime(_snapshot(), refresh_error=AssertionError("runtime 程序错误"))
    _wire(
        monkeypatch,
        config=_config(),
        runtime=runtime,
        provider=_EmbeddingProvider(),
    )
    with pytest.raises(AssertionError, match="runtime 程序错误"):
        await decision_plugin.classify_summary_request("总结一下今天聊了什么")

    provider = _EmbeddingProvider(
        rerank_error=RuntimeError("未声明的 rerank 程序错误")
    )
    _wire(
        monkeypatch,
        config=_config(),
        runtime=_Runtime(_snapshot()),
        provider=provider,
    )
    with pytest.raises(RuntimeError, match="未声明的 rerank 程序错误"):
        await decision_plugin.classify_summary_request("总结一下今天聊了什么")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        RemoteServiceRequestError("rerank_api 请求失败"),
        RerankResponseValidationError("rerank 响应无效"),
        RuntimeError("EmbeddingProvider 尚未初始化"),
    ],
)
async def test_expected_rerank_failures_are_safe(
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
) -> None:
    _wire(
        monkeypatch,
        config=_config(),
        runtime=_Runtime(_snapshot()),
        provider=_EmbeddingProvider(rerank_error=error),
    )

    result = await decision_plugin.classify_summary_request(
        "总结一下今天聊了什么"
    )

    assert result == SummaryRequestClassificationResult.unavailable(
        SummaryRequestUnavailableReason.RERANK_UNAVAILABLE
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        TypeError("程序错误"),
        RuntimeError("未声明的程序错误"),
        asyncio.CancelledError(),
    ],
)
async def test_programming_errors_and_cancellation_propagate(
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
) -> None:
    provider = _EmbeddingProvider(embed_error=error)
    _wire(
        monkeypatch,
        config=_config(),
        runtime=_Runtime(_snapshot()),
        provider=provider,
    )

    with pytest.raises(type(error)):
        await decision_plugin.classify_summary_request("总结一下今天聊了什么")
