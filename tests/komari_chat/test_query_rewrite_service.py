"""查询重写服务测试。"""

from __future__ import annotations

import asyncio
from importlib import import_module
from types import SimpleNamespace
from typing import Any

from komari_bot.plugins.komari_memory.core import retry as retry_module

query_rewrite_module = import_module(
    "komari_bot.plugins.komari_chat.services.query_rewrite_service"
)


class _FakeLLMProvider:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls = 0
        self.completion_calls: list[dict[str, object]] = []

    async def generate_completion(self, **kwargs: object) -> SimpleNamespace:
        self.calls += 1
        self.completion_calls.append(kwargs)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(
            content=str(response),
            finish_reason="stop",
            duration_ms=50.0,
            usage=None,
        )


async def _no_sleep(_delay: float) -> None:
    return None


def _patch_config(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        query_rewrite_module,
        "get_memory_config",
        lambda: SimpleNamespace(
            llm_model_summary="summary-model",
            llm_thinking_mode_summary=False,
            llm_reasoning_effort_summary="",
        ),
    )
    monkeypatch.setattr(retry_module.asyncio, "sleep", _no_sleep)


def test_rewrite_query_returns_original_for_blank_input(monkeypatch: Any) -> None:
    _patch_config(monkeypatch)
    fake_provider = _FakeLLMProvider(["不会被调用"])
    monkeypatch.setattr(query_rewrite_module, "llm_provider", fake_provider)

    service = query_rewrite_module.QueryRewriteService()
    result = asyncio.run(service.rewrite_query("   "))

    assert result == "   "
    assert fake_provider.calls == 0


def test_rewrite_query_retries_then_succeeds(monkeypatch: Any) -> None:
    _patch_config(monkeypatch)
    fake_provider = _FakeLLMProvider([RuntimeError("boom"), "  她刚才提到的角色是谁  "])
    monkeypatch.setattr(query_rewrite_module, "llm_provider", fake_provider)

    service = query_rewrite_module.QueryRewriteService()
    result = asyncio.run(service.rewrite_query("她是谁"))

    assert result == "她刚才提到的角色是谁"
    assert fake_provider.calls == 2


def test_rewrite_query_falls_back_after_all_retries_fail(monkeypatch: Any) -> None:
    _patch_config(monkeypatch)
    fake_provider = _FakeLLMProvider([RuntimeError("boom"), RuntimeError("again")])
    monkeypatch.setattr(query_rewrite_module, "llm_provider", fake_provider)

    service = query_rewrite_module.QueryRewriteService()
    result = asyncio.run(service.rewrite_query("她是谁"))

    assert result == "她是谁"
    assert fake_provider.calls == 2


def test_rewrite_query_falls_back_for_invalid_output(monkeypatch: Any) -> None:
    _patch_config(monkeypatch)
    fake_provider = _FakeLLMProvider(["x" * 201])
    monkeypatch.setattr(query_rewrite_module, "llm_provider", fake_provider)

    service = query_rewrite_module.QueryRewriteService()
    result = asyncio.run(service.rewrite_query("她是谁"))

    assert result == "她是谁"


def test_rewrite_query_prompt_only_includes_current_input(monkeypatch: Any) -> None:
    _patch_config(monkeypatch)
    fake_provider = _FakeLLMProvider(["她刚才提到的角色是谁"])
    monkeypatch.setattr(query_rewrite_module, "llm_provider", fake_provider)

    service = query_rewrite_module.QueryRewriteService()
    result = asyncio.run(service.rewrite_query("她是谁"))

    assert result == "她刚才提到的角色是谁"
    prompt = str(fake_provider.completion_calls[0]["prompt"])
    assert "用户输入：她是谁" in prompt
    assert "对话历史：" not in prompt
    assert "引用消息：" not in prompt


def test_rewrite_query_still_calls_llm_for_non_empty_input(monkeypatch: Any) -> None:
    _patch_config(monkeypatch)
    fake_provider = _FakeLLMProvider(["帮我看看这张图是什么"])
    monkeypatch.setattr(query_rewrite_module, "llm_provider", fake_provider)

    service = query_rewrite_module.QueryRewriteService()
    result = asyncio.run(service.rewrite_query("这个呢"))

    assert result == "帮我看看这张图是什么"
    assert fake_provider.calls == 1
    assert fake_provider.completion_calls[0]["request_phase"] == "query_rewrite"


def test_rewrite_query_records_trace_in_collector(monkeypatch: Any) -> None:
    """查询重写在使用 completion 接口时记录 trace 到 collector。"""
    _patch_config(monkeypatch)
    fake_provider = _FakeLLMProvider(["重写后的查询"])
    monkeypatch.setattr(query_rewrite_module, "llm_provider", fake_provider)
    from komari_bot.plugins.agent_run_logger.diagnostic import LLMDiagnosticCollector

    collector = LLMDiagnosticCollector(request_id="test-qr")
    service = query_rewrite_module.QueryRewriteService()
    result = asyncio.run(
        service.rewrite_query(
            "她是谁",
            request_trace_id="trace-qr",
            parent_call_id="parent-qr",
            collector=collector,
        )
    )

    assert result == "重写后的查询"
    assert len(collector.calls) == 1
    assert collector.calls[0].phase == "query_rewrite"
    assert collector.calls[0].parent_call_id == "parent-qr"
