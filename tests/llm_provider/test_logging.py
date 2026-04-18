"""LLM Provider 聊天日志开关测试。"""

from __future__ import annotations

import asyncio
from importlib import import_module
from typing import Any

llm_provider_module = import_module("komari_bot.plugins.llm_provider.__init__")


class _FakeClient:
    def __init__(self, response: str) -> None:
        self.response = response
        self.closed = False

    async def generate_text(self, **_kwargs: Any) -> str:
        return self.response

    async def generate_text_with_messages(self, **_kwargs: Any) -> str:
        return self.response

    async def close(self) -> None:
        self.closed = True


def test_generate_text_does_not_record_log_by_default(monkeypatch: Any) -> None:
    fake_client = _FakeClient("普通调用结果")
    log_calls: list[dict[str, Any]] = []

    async def _fake_log_llm_call(**kwargs: Any) -> None:
        log_calls.append(kwargs)

    monkeypatch.setattr(llm_provider_module, "_get_client", lambda: fake_client)
    monkeypatch.setattr(llm_provider_module, "log_llm_call", _fake_log_llm_call)

    result = asyncio.run(
        llm_provider_module.generate_text(
            prompt="这是一段内部总结请求",
            model="deepseek-chat",
        )
    )

    assert result == "普通调用结果"
    assert fake_client.closed is True
    assert log_calls == []


def test_generate_text_with_messages_records_log_for_chat_reply(
    monkeypatch: Any,
) -> None:
    fake_client = _FakeClient("<content>聊天回复</content>")
    log_calls: list[dict[str, Any]] = []

    async def _fake_log_llm_call(**kwargs: Any) -> None:
        log_calls.append(kwargs)

    monkeypatch.setattr(llm_provider_module, "_get_client", lambda: fake_client)
    monkeypatch.setattr(llm_provider_module, "log_llm_call", _fake_log_llm_call)

    result = asyncio.run(
        llm_provider_module.generate_text_with_messages(
            messages=[{"role": "user", "content": "你好"}],
            model="deepseek-chat",
            request_trace_id="chat-1001",
            record_chat_log=True,
        )
    )

    assert result == "<content>聊天回复</content>"
    assert fake_client.closed is True
    assert len(log_calls) == 1
    assert log_calls[0]["method"] == "generate_text_with_messages"
    assert log_calls[0]["input_data"]["trace_id"] == "chat-1001"

