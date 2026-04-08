"""JRHG LLM 调用测试。"""

from __future__ import annotations

import asyncio
from importlib import import_module
from types import SimpleNamespace
from typing import Any

llm_service_module = import_module("komari_bot.plugins.jrhg.llm_service")


class _FakeLLMProvider:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def generate_text_with_messages(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return self.response


def test_generate_reply_extracts_content_tag(monkeypatch: Any) -> None:
    fake_provider = _FakeLLMProvider(
        "<think>先想想</think><content>今、今天就陪你一下……</content>"
    )
    monkeypatch.setattr(llm_service_module, "llm_provider", fake_provider)
    monkeypatch.setattr(
        llm_service_module,
        "get_config",
        lambda: SimpleNamespace(
            llm_model="jrhg-model",
            llm_temperature=0.6,
            llm_max_tokens=512,
        ),
    )

    result = asyncio.run(
        llm_service_module.generate_reply(
            messages=[{"role": "user", "content": "你好"}],
            request_trace_id="jrhg-1",
        )
    )

    assert result == "今、今天就陪你一下……"
    assert fake_provider.calls[0]["model"] == "jrhg-model"
    assert fake_provider.calls[0]["temperature"] == 0.6
    assert fake_provider.calls[0]["max_tokens"] == 512
    assert fake_provider.calls[0]["request_trace_id"] == "jrhg-1"


def test_generate_reply_falls_back_to_raw_text_when_tag_missing(
    monkeypatch: Any,
) -> None:
    fake_provider = _FakeLLMProvider("<think>先想想</think>去、去死啦。")
    monkeypatch.setattr(llm_service_module, "llm_provider", fake_provider)
    monkeypatch.setattr(
        llm_service_module,
        "get_config",
        lambda: SimpleNamespace(
            llm_model="jrhg-model",
            llm_temperature=0.6,
            llm_max_tokens=512,
        ),
    )

    result = asyncio.run(
        llm_service_module.generate_reply(
            messages=[{"role": "user", "content": "你好"}],
        )
    )

    assert result == "去、去死啦。"
