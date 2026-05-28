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
        self.prompts: list[str] = []

    async def generate_text(self, **kwargs: object) -> str:
        self.calls += 1
        self.prompts.append(str(kwargs.get("prompt", "")))
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return str(response)


async def _no_sleep(_delay: float) -> None:
    return None


def _patch_config(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        query_rewrite_module,
        "get_config",
        lambda: SimpleNamespace(
            llm_model_summary="summary-model",
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
    assert "用户输入：她是谁" in fake_provider.prompts[0]
    assert "对话历史：" not in fake_provider.prompts[0]
    assert "引用消息：" not in fake_provider.prompts[0]


def test_rewrite_query_still_calls_llm_for_non_empty_input(monkeypatch: Any) -> None:
    _patch_config(monkeypatch)
    fake_provider = _FakeLLMProvider(["帮我看看这张图是什么"])
    monkeypatch.setattr(query_rewrite_module, "llm_provider", fake_provider)

    service = query_rewrite_module.QueryRewriteService()
    result = asyncio.run(service.rewrite_query("这个呢"))

    assert result == "帮我看看这张图是什么"
    assert fake_provider.calls == 1
