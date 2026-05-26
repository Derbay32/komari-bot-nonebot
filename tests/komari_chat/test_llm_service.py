"""Komari Chat LLM 服务测试。"""

from __future__ import annotations

import asyncio
from importlib import import_module
from types import SimpleNamespace
from typing import Any

llm_service_module = import_module("komari_bot.plugins.komari_chat.services.llm_service")


class _FakeLLMProvider:
    def __init__(self, response: str) -> None:
        self.response = response
        self.message_calls: list[dict[str, Any]] = []
        self.text_calls: list[dict[str, Any]] = []
        self.completion_calls: list[dict[str, Any]] = []
        self.completions: list[SimpleNamespace] = []

    async def generate_text_with_messages(self, **kwargs: Any) -> str:
        self.message_calls.append(kwargs)
        return self.response

    async def generate_text(self, **kwargs: Any) -> str:
        self.text_calls.append(kwargs)
        return self.response

    async def generate_messages_completion(self, **kwargs: Any) -> SimpleNamespace:
        self.completion_calls.append(kwargs)
        return self.completions.pop(0)


def _build_config() -> SimpleNamespace:
    return SimpleNamespace(
        llm_model_chat="chat-model",
        llm_temperature_chat=0.7,
        llm_max_tokens_chat=1024,
        response_tag="content",
    )


def test_generate_reply_enables_chat_log_for_messages(monkeypatch: Any) -> None:
    fake_provider = _FakeLLMProvider("<content>今天就陪陪Master</content>")
    monkeypatch.setattr(llm_service_module, "llm_provider", fake_provider)

    result = asyncio.run(
        llm_service_module.generate_reply(
            config=_build_config(),
            messages=[{"role": "user", "content": "你好"}],
            request_trace_id="chat-2001",
        )
    )

    assert result == "今天就陪陪Master"
    assert fake_provider.message_calls[0]["record_chat_log"] is True
    assert fake_provider.message_calls[0]["request_trace_id"] == "chat-2001"


def test_generate_reply_enables_chat_log_for_legacy_prompt(monkeypatch: Any) -> None:
    fake_provider = _FakeLLMProvider("<content>旧接口也要记聊天日志</content>")
    monkeypatch.setattr(llm_service_module, "llm_provider", fake_provider)

    result = asyncio.run(
        llm_service_module.generate_reply(
            config=_build_config(),
            user_message="你好",
            system_prompt="你是助手",
            request_trace_id="chat-2002",
        )
    )

    assert result == "旧接口也要记聊天日志"
    assert fake_provider.text_calls[0]["record_chat_log"] is True
    assert fake_provider.text_calls[0]["request_trace_id"] == "chat-2002"


def test_generate_reply_with_search_tool_executes_tool_loop(monkeypatch: Any) -> None:
    fake_provider = _FakeLLMProvider("<content>搜索后的最终回答</content>")
    fake_provider.completions = [
        SimpleNamespace(
            content="",
            tool_calls=[
                SimpleNamespace(
                    id="call-1",
                    type="function",
                    function=SimpleNamespace(
                        name="search_web",
                        arguments='{"query":"今日新闻"}',
                    ),
                    raw_arguments='{"query":"今日新闻"}',
                    parsed_arguments={"query": "今日新闻"},
                )
            ],
        ),
        SimpleNamespace(
            content="<content>根据搜索结果回答</content>",
            tool_calls=[],
        ),
    ]
    searched_queries: list[str] = []

    async def _fake_search_web(query: str) -> str:
        searched_queries.append(query)
        return "搜索结果：今天有一条新闻"

    monkeypatch.setattr(llm_service_module, "llm_provider", fake_provider)
    monkeypatch.setattr(
        llm_service_module,
        "komari_search",
        SimpleNamespace(search_web=_fake_search_web),
    )

    result = asyncio.run(
        llm_service_module.generate_reply_with_search_tool(
            config=_build_config(),
            messages=[{"role": "user", "content": "查一下今日新闻"}],
            request_trace_id="chat-search-1",
        )
    )

    assert result == "根据搜索结果回答"
    assert searched_queries == ["今日新闻"]
    assert fake_provider.completion_calls[0]["tools"] == [
        llm_service_module.TAVILY_SEARCH_TOOL
    ]
    assert fake_provider.completion_calls[1]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "搜索结果：今天有一条新闻",
    }
