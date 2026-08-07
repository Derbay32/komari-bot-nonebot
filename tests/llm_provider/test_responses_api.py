"""Responses API 路径测试（假 responses.create 端点 + 假流事件生成器）。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from komari_bot.llm.untrusted_context import (
    LLM_SECURITY_SYSTEM_INSTRUCTION,
    UntrustedContext,
)
from komari_bot.plugins.llm_provider import openai_compatible_api as openai_api_module
from komari_bot.plugins.llm_provider.base_client import CONTINUATION_METADATA_KEY
from komari_bot.plugins.llm_provider.openai_compatible_api import (
    OpenAICompatibleClient,
)


def _patch_config_manager(monkeypatch: Any, **overrides: Any) -> None:
    """注入测试用 config_manager，返回带默认字段的 SimpleNamespace 配置。"""
    base: dict[str, Any] = {
        "temperature": 1.0,
        "max_tokens": 8192,
        "frequency_penalty": 0.0,
        "api_base": "https://example.com/v1",
        "extra_params": {},
        "request_api": "chat_completions",
        "stream_enabled": False,
    }
    base.update(overrides)
    monkeypatch.setattr(
        openai_api_module,
        "config_manager",
        SimpleNamespace(get=lambda: SimpleNamespace(**base)),
    )


def _text_message_item(text: str) -> Any:
    return SimpleNamespace(
        type="message",
        role="assistant",
        content=[SimpleNamespace(type="output_text", text=text)],
    )


def _function_call_item(
    name: str,
    arguments: str,
    *,
    call_id: str = "call_1",
) -> Any:
    return SimpleNamespace(
        type="function_call",
        call_id=call_id,
        name=name,
        arguments=arguments,
    )


def _completed_response(
    output: list[Any],
    *,
    usage: Any = None,
) -> Any:
    return SimpleNamespace(status="completed", output=output, usage=usage)


def _default_usage() -> Any:
    return SimpleNamespace(
        input_tokens=120,
        input_tokens_details=SimpleNamespace(cached_tokens=70),
        output_tokens=30,
        output_tokens_details=SimpleNamespace(reasoning_tokens=12),
        total_tokens=150,
    )


class _FakeResponses:
    """记录请求并返回预设终态的假 responses 端点。"""

    def __init__(self, *, response: Any = None, stream: Any = None) -> None:
        self.last_kwargs: dict[str, Any] | None = None
        self._response = response
        self._stream = stream

    async def create(self, **kwargs: Any) -> Any:
        self.last_kwargs = kwargs
        if kwargs.get("stream"):
            assert self._stream is not None
            return self._stream
        assert self._response is not None
        return self._response


class _FakeSDKClient:
    def __init__(self, responses: _FakeResponses) -> None:
        self.responses = responses

    async def close(self) -> None:
        return None


def _make_client(
    monkeypatch: Any,
    responses: _FakeResponses,
    **config_overrides: Any,
) -> Any:
    client = OpenAICompatibleClient(
        "token",
        base_url="https://example.com/v1",
        timeout_seconds=300.0,
    )
    monkeypatch.setattr(client, "client", _FakeSDKClient(responses))
    _patch_config_manager(monkeypatch, **config_overrides)
    return client


# ==================== 请求构造 ====================


def test_responses_request_merges_system_into_instructions(
    monkeypatch: Any,
) -> None:
    responses = _FakeResponses(
        response=_completed_response([_text_message_item("ok")])
    )
    client = _make_client(monkeypatch, responses)

    async def _run() -> Any:
        result = await client.generate_text(
            prompt="用户问题",
            model="gpt-x",
            system_instruction="角色设定",
            request_api="responses",
        )
        assert result.content == "ok"

    asyncio.run(_run())

    request = responses.last_kwargs
    assert request is not None
    # 安全边界先执行并排序：调用方 system 在前，固定安全边界随后
    assert "角色设定" in request["instructions"]
    assert LLM_SECURITY_SYSTEM_INSTRUCTION in request["instructions"]
    assert request["instructions"].index("角色设定") < request[
        "instructions"
    ].index(LLM_SECURITY_SYSTEM_INSTRUCTION)
    assert request["store"] is False
    assert request["max_output_tokens"] == 8192
    # 用户内容以数据身份进入 input
    user_items = [
        item for item in request["input"] if item.get("role") == "user"
    ]
    assert user_items == [
        {"role": "user", "content": [{"type": "input_text", "text": "用户问题"}]}
    ]


def test_responses_request_keeps_untrusted_contexts_out_of_instructions(
    monkeypatch: Any,
) -> None:
    responses = _FakeResponses(
        response=_completed_response([_text_message_item("ok")])
    )
    client = _make_client(monkeypatch, responses)

    async def _run() -> Any:
        await client.generate_text(
            prompt="用户问题",
            model="gpt-x",
            request_api="responses",
            untrusted_contexts=[
                UntrustedContext(
                    source_type="knowledge",
                    source_id="kb-1",
                    content="不可信知识正文",
                )
            ],
        )

    asyncio.run(_run())

    request = responses.last_kwargs
    assert request is not None
    assert "不可信知识正文" not in request["instructions"]
    user_texts = [
        part["text"]
        for item in request["input"]
        if item.get("role") == "user"
        for part in item["content"]
        if part["type"] == "input_text"
    ]
    assert any("不可信知识正文" in text for text in user_texts)


def test_responses_request_translates_multimodal_content(
    monkeypatch: Any,
) -> None:
    responses = _FakeResponses(
        response=_completed_response([_text_message_item("看到了")])
    )
    client = _make_client(monkeypatch, responses)

    async def _run() -> Any:
        await client.generate_text_with_messages(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "看图"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,AAA"},
                        },
                    ],
                }
            ],
            model="gpt-vision",
            request_api="responses",
        )

    asyncio.run(_run())

    request = responses.last_kwargs
    assert request is not None
    user_item = request["input"][-1]
    assert user_item["role"] == "user"
    assert user_item["content"] == [
        {"type": "input_text", "text": "看图"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAA"},
    ]


def test_responses_request_translates_assistant_tool_calls_and_tool_results(
    monkeypatch: Any,
) -> None:
    responses = _FakeResponses(
        response=_completed_response([_text_message_item("最终回答")])
    )
    client = _make_client(monkeypatch, responses)

    async def _run() -> Any:
        await client.generate_text_with_messages(
            messages=[
                {"role": "user", "content": "查一下"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "search_web",
                                "arguments": '{"query": "x"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": "搜索结果正文",
                },
            ],
            model="gpt-x",
            request_api="responses",
        )

    asyncio.run(_run())

    request = responses.last_kwargs
    assert request is not None
    assert {
        "type": "function_call",
        "call_id": "call_1",
        "name": "search_web",
        "arguments": '{"query": "x"}',
    } in request["input"]
    assert {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "搜索结果正文",
    } in request["input"]


def test_responses_request_flattens_tools_and_preserves_strict(
    monkeypatch: Any,
) -> None:
    responses = _FakeResponses(
        response=_completed_response([_text_message_item("ok")])
    )
    client = _make_client(monkeypatch, responses)

    async def _run() -> Any:
        await client.generate_text_with_messages(
            messages=[{"role": "user", "content": "你好"}],
            model="gpt-x",
            request_api="responses",
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "tool_a",
                        "description": "工具 A",
                        "parameters": {"type": "object"},
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "tool_b",
                        "parameters": {"type": "object"},
                        "strict": True,
                    },
                },
            ],
        )

    asyncio.run(_run())

    request = responses.last_kwargs
    assert request is not None
    assert request["tools"] == [
        {
            "type": "function",
            "name": "tool_a",
            "description": "工具 A",
            "parameters": {"type": "object"},
            "strict": False,
        },
        {
            "type": "function",
            "name": "tool_b",
            "parameters": {"type": "object"},
            "strict": True,
        },
    ]
    # 存在函数工具时请求加密推理内容
    assert request["include"] == ["reasoning.encrypted_content"]


def test_responses_request_translates_tool_choice_and_parallel(
    monkeypatch: Any,
) -> None:
    responses = _FakeResponses(
        response=_completed_response([_text_message_item("ok")])
    )
    client = _make_client(monkeypatch, responses)

    async def _run() -> Any:
        await client.generate_text_with_messages(
            messages=[{"role": "user", "content": "你好"}],
            model="gpt-x",
            request_api="responses",
            tool_choice={
                "type": "function",
                "function": {"name": "record_favorability"},
            },
            parallel_tool_calls=False,
        )

    asyncio.run(_run())

    request = responses.last_kwargs
    assert request is not None
    assert request["tool_choice"] == {
        "type": "function",
        "name": "record_favorability",
    }
    assert request["parallel_tool_calls"] is False


def test_responses_request_translates_response_format(
    monkeypatch: Any,
) -> None:
    responses = _FakeResponses(
        response=_completed_response([_text_message_item("{}")])
    )
    client = _make_client(monkeypatch, responses)

    async def _run() -> Any:
        await client.generate_text(
            prompt="返回 JSON",
            model="gpt-x",
            request_api="responses",
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "summary",
                    "schema": {"type": "object"},
                    "strict": True,
                },
            },
        )

    asyncio.run(_run())

    request = responses.last_kwargs
    assert request is not None
    assert request["text"] == {
        "format": {
            "type": "json_schema",
            "name": "summary",
            "schema": {"type": "object"},
            "strict": True,
        }
    }


def test_responses_request_maps_reasoning_effort(monkeypatch: Any) -> None:
    responses = _FakeResponses(
        response=_completed_response([_text_message_item("ok")])
    )
    client = _make_client(monkeypatch, responses)

    async def _run() -> Any:
        await client.generate_text(
            prompt="思考",
            model="gpt-x",
            request_api="responses",
            thinking_mode=True,
            reasoning_effort="high",
        )

    asyncio.run(_run())

    request = responses.last_kwargs
    assert request is not None
    assert request["reasoning"] == {"effort": "high"}


def test_responses_request_silently_ignores_unmapped_sampling_params(
    monkeypatch: Any,
) -> None:
    """frequency_penalty 与不兼容 extra_params 键静默忽略；top_p 直通。"""
    responses = _FakeResponses(
        response=_completed_response([_text_message_item("ok")])
    )
    client = _make_client(
        monkeypatch,
        responses,
        frequency_penalty=0.5,
        extra_params={"top_p": 0.9, "min_p": 0.1, "seed": 42},
    )

    async def _run() -> Any:
        await client.generate_text(
            prompt="你好",
            model="gpt-x",
            request_api="responses",
        )

    asyncio.run(_run())

    request = responses.last_kwargs
    assert request is not None
    assert "frequency_penalty" not in request
    assert "extra_body" not in request
    assert "min_p" not in request
    assert "seed" not in request
    assert request["top_p"] == 0.9


# ==================== 终态归一化 ====================


def test_responses_completed_with_function_call_maps_tool_calls(
    monkeypatch: Any,
) -> None:
    responses = _FakeResponses(
        response=_completed_response(
            [_function_call_item("search_web", '{"query": "x"}', call_id="call_9")],
            usage=_default_usage(),
        )
    )
    client = _make_client(monkeypatch, responses)

    async def _run() -> Any:
        return await client.generate_text(
            prompt="搜索", model="gpt-x", request_api="responses"
        )

    result = asyncio.run(_run())

    assert result.finish_reason == "tool_calls"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].id == "call_9"
    assert result.tool_calls[0].function.name == "search_web"
    assert result.tool_calls[0].parsed_arguments == {"query": "x"}
    assert result.usage is not None
    assert result.usage.input_tokens == 120
    assert result.usage.cached_input_tokens == 70
    assert result.usage.output_tokens == 30
    assert result.usage.reasoning_output_tokens == 12
    assert result.usage.total_tokens == 150


def test_responses_completed_plain_maps_stop_and_continuation(
    monkeypatch: Any,
) -> None:
    reasoning_item = SimpleNamespace(
        type="reasoning", id="rs_1", encrypted_content="密文推理"
    )
    responses = _FakeResponses(
        response=_completed_response(
            [reasoning_item, _text_message_item("回答正文")],
            usage=_default_usage(),
        )
    )
    client = _make_client(monkeypatch, responses)

    async def _run() -> Any:
        return await client.generate_text(
            prompt="你好", model="gpt-x", request_api="responses"
        )

    result = asyncio.run(_run())

    assert result.content == "回答正文"
    assert result.finish_reason == "stop"
    # 续接项携带全部 output items（含加密推理项），原样可回传
    assert result.continuation is not None
    assert result.continuation.api == "responses"
    assert result.continuation.output_items[0]["type"] == "reasoning"
    assert result.continuation.output_items[0]["encrypted_content"] == "密文推理"
    assert result.continuation.output_items[1]["type"] == "message"


def test_responses_refusal_merges_into_content_with_content_filter(
    monkeypatch: Any,
) -> None:
    refusal_item = SimpleNamespace(
        type="message",
        role="assistant",
        content=[SimpleNamespace(type="refusal", refusal="无法协助该请求")],
    )
    responses = _FakeResponses(response=_completed_response([refusal_item]))
    client = _make_client(monkeypatch, responses)

    async def _run() -> Any:
        return await client.generate_text(
            prompt="危险请求", model="gpt-x", request_api="responses"
        )

    result = asyncio.run(_run())

    assert result.content == "无法协助该请求"
    assert result.finish_reason == "content_filter"


def test_responses_incomplete_max_output_tokens_maps_length(
    monkeypatch: Any,
) -> None:
    response = SimpleNamespace(
        status="incomplete",
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
        output=[_text_message_item("截断的内容")],
        usage=_default_usage(),
    )
    responses = _FakeResponses(response=response)
    client = _make_client(monkeypatch, responses)

    async def _run() -> Any:
        return await client.generate_text(
            prompt="长文", model="gpt-x", request_api="responses"
        )

    result = asyncio.run(_run())

    assert result.content == "截断的内容"
    assert result.finish_reason == "length"
    # 合法截断保留已上报 usage
    assert result.usage is not None
    assert result.usage.total_tokens == 150


def test_responses_incomplete_content_filter_maps_content_filter(
    monkeypatch: Any,
) -> None:
    response = SimpleNamespace(
        status="incomplete",
        incomplete_details=SimpleNamespace(reason="content_filter"),
        output=[_text_message_item("被过滤的部分")],
        usage=None,
    )
    responses = _FakeResponses(response=response)
    client = _make_client(monkeypatch, responses)

    async def _run() -> Any:
        return await client.generate_text(
            prompt="敏感", model="gpt-x", request_api="responses"
        )

    result = asyncio.run(_run())

    assert result.content == "被过滤的部分"
    assert result.finish_reason == "content_filter"
    assert result.usage is None


def test_responses_incomplete_unknown_reason_raises(monkeypatch: Any) -> None:
    response = SimpleNamespace(
        status="incomplete",
        incomplete_details=SimpleNamespace(reason="mystery"),
        output=[_text_message_item("部分")],
        usage=None,
    )
    responses = _FakeResponses(response=response)
    client = _make_client(monkeypatch, responses)

    async def _run() -> Any:
        with pytest.raises(RuntimeError, match="响应状态异常"):
            await client.generate_text(
                prompt="你好", model="gpt-x", request_api="responses"
            )

    asyncio.run(_run())


@pytest.mark.parametrize("status", ["failed", "cancelled", "queued", "in_progress"])
def test_responses_non_terminal_status_raises(
    monkeypatch: Any, status: str
) -> None:
    response = SimpleNamespace(
        status=status,
        output=[_text_message_item("部分")],
        usage=None,
    )
    responses = _FakeResponses(response=response)
    client = _make_client(monkeypatch, responses)

    async def _run() -> Any:
        with pytest.raises(RuntimeError, match="响应状态异常"):
            await client.generate_text(
                prompt="你好", model="gpt-x", request_api="responses"
            )

    asyncio.run(_run())


# ==================== 流式路径 ====================


class _FakeEventStream:
    """假 Responses SSE 事件流。"""

    def __init__(
        self,
        events: list[Any],
        *,
        error: BaseException | None = None,
    ) -> None:
        self._events = events
        self._error = error
        self.closed = False

    def __aiter__(self) -> Any:
        return self._iterate()

    async def _iterate(self) -> Any:
        for event in self._events:
            yield event
        if self._error is not None:
            raise self._error

    async def close(self) -> None:
        self.closed = True


def _terminal_event(event_type: str, response: Any) -> Any:
    return SimpleNamespace(type=event_type, response=response)


def test_responses_stream_completed_shares_terminal_parser(
    monkeypatch: Any,
) -> None:
    final_response = _completed_response(
        [_text_message_item("流式回答")],
        usage=_default_usage(),
    )
    stream = _FakeEventStream(
        [
            SimpleNamespace(type="response.output_text.delta", delta="流式"),
            _terminal_event("response.completed", final_response),
        ]
    )
    responses = _FakeResponses(stream=stream)
    client = _make_client(monkeypatch, responses)

    async def _run() -> Any:
        return await client.generate_text(
            prompt="你好",
            model="gpt-x",
            request_api="responses",
            stream_enabled=True,
        )

    result = asyncio.run(_run())

    assert result.content == "流式回答"
    assert result.finish_reason == "stop"
    assert result.usage is not None
    assert result.usage.total_tokens == 150
    assert result.continuation is not None
    assert stream.closed is True
    request = responses.last_kwargs
    assert request is not None
    assert request["stream"] is True


def test_responses_stream_incomplete_maps_length(monkeypatch: Any) -> None:
    final_response = SimpleNamespace(
        status="incomplete",
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
        output=[_text_message_item("流式截断")],
        usage=None,
    )
    stream = _FakeEventStream(
        [_terminal_event("response.incomplete", final_response)]
    )
    responses = _FakeResponses(stream=stream)
    client = _make_client(monkeypatch, responses)

    async def _run() -> Any:
        return await client.generate_text(
            prompt="长文",
            model="gpt-x",
            request_api="responses",
            stream_enabled=True,
        )

    result = asyncio.run(_run())

    assert result.content == "流式截断"
    assert result.finish_reason == "length"


def test_responses_stream_error_event_raises_and_closes(
    monkeypatch: Any,
) -> None:
    stream = _FakeEventStream([SimpleNamespace(type="error", code="server_error")])
    responses = _FakeResponses(stream=stream)
    client = _make_client(monkeypatch, responses)

    async def _run() -> Any:
        with pytest.raises(RuntimeError, match="响应状态异常"):
            await client.generate_text(
                prompt="你好",
                model="gpt-x",
                request_api="responses",
                stream_enabled=True,
            )

    asyncio.run(_run())
    assert stream.closed is True


def test_responses_stream_failed_event_raises(monkeypatch: Any) -> None:
    final_response = SimpleNamespace(status="failed", output=[], usage=None)
    stream = _FakeEventStream(
        [_terminal_event("response.failed", final_response)]
    )
    responses = _FakeResponses(stream=stream)
    client = _make_client(monkeypatch, responses)

    async def _run() -> Any:
        with pytest.raises(RuntimeError, match="响应状态异常"):
            await client.generate_text(
                prompt="你好",
                model="gpt-x",
                request_api="responses",
                stream_enabled=True,
            )

    asyncio.run(_run())
    assert stream.closed is True


def test_responses_stream_disconnect_without_terminal_raises(
    monkeypatch: Any,
) -> None:
    stream = _FakeEventStream(
        [SimpleNamespace(type="response.output_text.delta", delta="部分")],
        error=RuntimeError("连接中断"),
    )
    responses = _FakeResponses(stream=stream)
    client = _make_client(monkeypatch, responses)

    async def _run() -> Any:
        with pytest.raises(RuntimeError, match="连接中断"):
            await client.generate_text(
                prompt="你好",
                model="gpt-x",
                request_api="responses",
                stream_enabled=True,
            )

    asyncio.run(_run())
    assert stream.closed is True


def test_responses_stream_missing_terminal_raises(monkeypatch: Any) -> None:
    stream = _FakeEventStream(
        [SimpleNamespace(type="response.output_text.delta", delta="部分")]
    )
    responses = _FakeResponses(stream=stream)
    client = _make_client(monkeypatch, responses)

    async def _run() -> Any:
        with pytest.raises(RuntimeError, match="响应状态异常"):
            await client.generate_text(
                prompt="你好",
                model="gpt-x",
                request_api="responses",
                stream_enabled=True,
            )

    asyncio.run(_run())
    assert stream.closed is True


# ==================== continuation 展开（ticket 07 接缝） ====================


def test_responses_request_expands_continuation_output_items(
    monkeypatch: Any,
) -> None:
    """Responses 构建器校验协议并原样展开 continuation，不重复重建 assistant。"""
    responses = _FakeResponses(
        response=_completed_response([_text_message_item("第二轮回答")])
    )
    client = _make_client(monkeypatch, responses)

    continuation = {
        "api": "responses",
        "output_items": [
            {"type": "reasoning", "id": "rs_1", "encrypted_content": "密文"},
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "search_web",
                "arguments": '{"query": "x"}',
            },
        ],
    }

    async def _run() -> Any:
        await client.generate_text_with_messages(
            messages=[
                {"role": "user", "content": "查一下"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "search_web",
                                "arguments": '{"query": "x"}',
                            },
                        }
                    ],
                    CONTINUATION_METADATA_KEY: continuation,
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": "结果",
                },
            ],
            model="gpt-x",
            request_api="responses",
        )

    asyncio.run(_run())

    request = responses.last_kwargs
    assert request is not None
    # 原样展开：推理项与函数调用项各一份，不重复重建 assistant 消息
    assert {
        "type": "reasoning",
        "id": "rs_1",
        "encrypted_content": "密文",
    } in request["input"]
    function_calls = [
        item for item in request["input"] if item.get("type") == "function_call"
    ]
    assert len(function_calls) == 1
    assistant_messages = [
        item for item in request["input"] if item.get("role") == "assistant"
    ]
    assert assistant_messages == []


def test_responses_request_rejects_foreign_continuation(
    monkeypatch: Any,
) -> None:
    responses = _FakeResponses(
        response=_completed_response([_text_message_item("ok")])
    )
    client = _make_client(monkeypatch, responses)

    async def _run() -> Any:
        with pytest.raises(RuntimeError, match="协议不匹配"):
            await client.generate_text_with_messages(
                messages=[
                    {"role": "user", "content": "你好"},
                    {
                        "role": "assistant",
                        "content": "上一轮",
                        CONTINUATION_METADATA_KEY: {
                            "api": "chat_completions",
                            "output_items": [],
                        },
                    },
                ],
                model="gpt-x",
                request_api="responses",
            )

    asyncio.run(_run())


def test_chat_request_strips_continuation_metadata(monkeypatch: Any) -> None:
    """Chat 构建器剥离 continuation 内部元数据，永不发往 Chat 端点。"""
    captured: dict[str, Any] = {}

    class _ChatCompletions:
        async def create(self, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
            )

    class _SDK:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(completions=_ChatCompletions())

        async def close(self) -> None:
            return None

    client = OpenAICompatibleClient(
        "token", base_url="https://example.com/v1", timeout_seconds=300.0
    )
    monkeypatch.setattr(client, "client", _SDK())
    _patch_config_manager(monkeypatch)

    continuation = {
        "api": "responses",
        "output_items": [{"type": "message", "id": "msg_1"}],
    }

    async def _run() -> Any:
        await client.generate_text_with_messages(
            messages=[
                {"role": "user", "content": "你好"},
                {
                    "role": "assistant",
                    "content": "上一轮回答",
                    CONTINUATION_METADATA_KEY: continuation,
                },
                {"role": "user", "content": "继续"},
            ],
            model="deepseek-chat",
            request_api="chat_completions",
        )

    asyncio.run(_run())

    wire_messages = captured["messages"]
    for message in wire_messages:
        assert CONTINUATION_METADATA_KEY not in message
        assert not any(key.startswith("_") for key in message)
    assert {
        "role": "assistant",
        "content": "上一轮回答",
    } in wire_messages
