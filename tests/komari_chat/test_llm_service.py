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


def test_generate_reply_with_tools_executes_search_tool_loop(monkeypatch: Any) -> None:
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
        llm_service_module.generate_reply_with_tools(
            config=_build_config(),
            messages=[{"role": "user", "content": "查一下今日新闻"}],
            tools=[llm_service_module.TAVILY_SEARCH_TOOL],
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


def test_generate_reply_with_tools_rejects_disabled_tool(monkeypatch: Any) -> None:
    fake_provider = _FakeLLMProvider("<content>兜底回答</content>")
    fake_provider.completions = [
        SimpleNamespace(
            content="",
            tool_calls=[
                SimpleNamespace(
                    id="call-image-1",
                    type="function",
                    function=SimpleNamespace(
                        name="read_image",
                        arguments='{"image_index":0}',
                    ),
                    raw_arguments='{"image_index":0}',
                    parsed_arguments={"image_index": 0},
                )
            ],
        ),
        SimpleNamespace(
            content="<content>没有启用图片工具</content>",
            tool_calls=[],
        ),
    ]
    read_image_called = False

    async def _fake_read_images(
        images: list[str],
        *,
        vision_model: str,
        temperature: float,
        max_tokens: int,
    ) -> list[str]:
        nonlocal read_image_called
        read_image_called = True
        assert images == []
        assert vision_model == ""
        assert temperature == 0.3
        assert max_tokens == 1024
        return ["不应读取图片"]

    monkeypatch.setattr(llm_service_module, "llm_provider", fake_provider)
    monkeypatch.setattr(llm_service_module, "read_images", _fake_read_images)

    result = asyncio.run(
        llm_service_module.generate_reply_with_tools(
            config=_build_config(),
            messages=[{"role": "user", "content": "看看图再搜索"}],
            tools=[llm_service_module.TAVILY_SEARCH_TOOL],
            request_trace_id="chat-disabled-tool-1",
        )
    )

    assert result == "没有启用图片工具"
    assert read_image_called is False
    assert fake_provider.completion_calls[1]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call-image-1",
        "content": "[工具调用失败: 当前未启用工具 read_image]",
    }


def test_generate_reply_with_tools_executes_combined_tools(monkeypatch: Any) -> None:
    fake_provider = _FakeLLMProvider("<content>组合工具最终回答</content>")
    fake_provider.completions = [
        SimpleNamespace(
            content="",
            tool_calls=[
                SimpleNamespace(
                    id="call-image-1",
                    type="function",
                    function=SimpleNamespace(
                        name="read_image",
                        arguments='{"image_index":0}',
                    ),
                    raw_arguments='{"image_index":0}',
                    parsed_arguments={"image_index": 0},
                ),
                SimpleNamespace(
                    id="call-search-1",
                    type="function",
                    function=SimpleNamespace(
                        name="search_web",
                        arguments='{"query":"天气"}',
                    ),
                    raw_arguments='{"query":"天气"}',
                    parsed_arguments={"query": "天气"},
                ),
            ],
        ),
        SimpleNamespace(
            content="<content>看图并搜索后的回答</content>",
            tool_calls=[],
        ),
    ]
    searched_queries: list[str] = []
    read_images_payloads: list[list[str]] = []

    async def _fake_read_images(
        images: list[str],
        *,
        vision_model: str,
        temperature: float,
        max_tokens: int,
    ) -> list[str]:
        read_images_payloads.append(images)
        assert vision_model == "vision-model"
        assert temperature == 0.2
        assert max_tokens == 512
        return ["图片描述：是一只猫"]

    async def _fake_search_web(query: str) -> str:
        searched_queries.append(query)
        return "搜索结果：天气晴"

    monkeypatch.setattr(llm_service_module, "llm_provider", fake_provider)
    monkeypatch.setattr(llm_service_module, "read_images", _fake_read_images)
    monkeypatch.setattr(
        llm_service_module,
        "komari_search",
        SimpleNamespace(search_web=_fake_search_web),
    )

    result = asyncio.run(
        llm_service_module.generate_reply_with_tools(
            config=_build_config(),
            messages=[{"role": "user", "content": "看图并查天气"}],
            tools=[
                llm_service_module.READ_IMAGE_TOOL,
                llm_service_module.TAVILY_SEARCH_TOOL,
            ],
            request_trace_id="chat-combined-tools-1",
            base64_images=["base64-image-0"],
            vision_model="vision-model",
            vision_temperature=0.2,
            vision_max_tokens=512,
        )
    )

    assert result == "看图并搜索后的回答"
    assert read_images_payloads == [["base64-image-0"]]
    assert searched_queries == ["天气"]
    assert fake_provider.completion_calls[0]["tools"] == [
        llm_service_module.READ_IMAGE_TOOL,
        llm_service_module.TAVILY_SEARCH_TOOL,
    ]
