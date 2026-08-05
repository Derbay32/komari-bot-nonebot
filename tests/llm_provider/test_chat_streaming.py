"""Chat Completions 流式聚合测试（假 SDK stream 接缝）。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from komari_bot.plugins.llm_provider import openai_compatible_api as openai_api_module
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


class _FakeStream:
    """假 SDK stream：异步迭代预设 chunk，可注入中途错误。"""

    def __init__(
        self,
        items: list[Any],
        *,
        error: BaseException | None = None,
    ) -> None:
        self._items = items
        self._error = error
        self.closed = False

    def __aiter__(self) -> Any:
        return self._iterate()

    async def _iterate(self) -> Any:
        for item in self._items:
            yield item
        if self._error is not None:
            raise self._error

    async def close(self) -> None:
        self.closed = True


def _text_chunk(text: str, *, finish_reason: str | None = None) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=text),
                finish_reason=finish_reason,
            )
        ]
    )


def _usage_chunk(
    *,
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
    total_tokens: int = 15,
) -> Any:
    """usage-only chunk：无 choices。"""
    return SimpleNamespace(
        choices=[],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        ),
    )


class _FakeCompletions:
    """记录请求并返回预设结果的假 chat.completions 端点。"""

    def __init__(self, *, stream: _FakeStream | None = None) -> None:
        self.last_kwargs: dict[str, Any] | None = None
        self._stream = stream

    async def create(self, **kwargs: Any) -> Any:
        self.last_kwargs = kwargs
        if kwargs.get("stream"):
            assert self._stream is not None
            return self._stream
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
        )


class _FakeSDKClient:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.chat = SimpleNamespace(completions=completions)

    async def close(self) -> None:
        return None


def _make_client(monkeypatch: Any, completions: _FakeCompletions) -> Any:
    client = OpenAICompatibleClient(
        "token",
        base_url="https://example.com/v1",
        timeout_seconds=300.0,
    )
    monkeypatch.setattr(client, "client", _FakeSDKClient(completions))
    _patch_config_manager(monkeypatch)
    return client


def test_chat_stream_request_sets_stream_and_include_usage(
    monkeypatch: Any,
) -> None:
    stream = _FakeStream([_text_chunk("你好", finish_reason="stop")])
    completions = _FakeCompletions(stream=stream)
    client = _make_client(monkeypatch, completions)

    async def _run() -> Any:
        result = await client.generate_text(
            prompt="你好", model="deepseek-chat", stream_enabled=True
        )
        assert result.content == "你好"
        assert result.finish_reason == "stop"

    asyncio.run(_run())

    request = completions.last_kwargs
    assert request is not None
    assert request["stream"] is True
    assert request["stream_options"] == {"include_usage": True}


def test_chat_stream_aggregates_text_reasoning_finish_and_usage(
    monkeypatch: Any,
) -> None:
    stream = _FakeStream(
        [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content="第一段", reasoning_content="思考一"
                        ),
                        finish_reason=None,
                    )
                ]
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content="第二段", reasoning_content="思考二"
                        ),
                        finish_reason=None,
                    )
                ]
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(),
                        finish_reason="stop",
                    )
                ]
            ),
            _usage_chunk(prompt_tokens=100, completion_tokens=40, total_tokens=140),
        ]
    )
    completions = _FakeCompletions(stream=stream)
    client = _make_client(monkeypatch, completions)

    async def _run() -> Any:
        return await client.generate_text_with_messages(
            messages=[{"role": "user", "content": "你好"}],
            model="deepseek-chat",
            stream_enabled=True,
        )

    result = asyncio.run(_run())

    assert result.content == "第一段第二段"
    assert result.reasoning_content == "思考一思考二"
    assert result.finish_reason == "stop"
    assert result.usage is not None
    assert result.usage.input_tokens == 100
    assert result.usage.output_tokens == 40
    assert result.usage.total_tokens == 140
    assert stream.closed is True


def test_chat_stream_aggregates_tool_calls_by_index(monkeypatch: Any) -> None:
    """并行工具调用的参数片段按 index 聚合，不交错错乱。"""

    def _tool_delta(
        index: int,
        *,
        call_id: str | None = None,
        name: str | None = None,
        arguments: str | None = None,
    ) -> Any:
        function = SimpleNamespace()
        if name is not None:
            function.name = name
        if arguments is not None:
            function.arguments = arguments
        delta = SimpleNamespace(index=index, function=function)
        if call_id is not None:
            delta.id = call_id
            delta.type = "function"
        return delta

    stream = _FakeStream(
        [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            tool_calls=[
                                _tool_delta(0, call_id="call_a", name="search"),
                                _tool_delta(1, call_id="call_b", name="fetch"),
                            ]
                        ),
                        finish_reason=None,
                    )
                ]
            ),
            # 交错到达的参数片段
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            tool_calls=[
                                _tool_delta(0, arguments='{"query": "'),
                                _tool_delta(1, arguments='{"url": "'),
                            ]
                        ),
                        finish_reason=None,
                    )
                ]
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            tool_calls=[
                                _tool_delta(1, arguments='a"}'),
                                _tool_delta(0, arguments='b"}'),
                            ]
                        ),
                        finish_reason=None,
                    )
                ]
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(),
                        finish_reason="tool_calls",
                    )
                ]
            ),
        ]
    )
    completions = _FakeCompletions(stream=stream)
    client = _make_client(monkeypatch, completions)

    async def _run() -> Any:
        return await client.generate_text_with_messages(
            messages=[{"role": "user", "content": "调用工具"}],
            model="deepseek-chat",
            stream_enabled=True,
        )

    result = asyncio.run(_run())

    assert result.finish_reason == "tool_calls"
    assert len(result.tool_calls) == 2
    assert result.tool_calls[0].id == "call_a"
    assert result.tool_calls[0].function.name == "search"
    assert result.tool_calls[0].raw_arguments == '{"query": "b"}'
    assert result.tool_calls[0].parsed_arguments == {"query": "b"}
    assert result.tool_calls[1].id == "call_b"
    assert result.tool_calls[1].function.name == "fetch"
    assert result.tool_calls[1].raw_arguments == '{"url": "a"}'


def test_chat_stream_without_usage_chunk_reports_missing_usage(
    monkeypatch: Any,
) -> None:
    """后端未报告 usage 时结果为缺失（None），禁止客户端估算。"""
    stream = _FakeStream([_text_chunk("内容", finish_reason="stop")])
    completions = _FakeCompletions(stream=stream)
    client = _make_client(monkeypatch, completions)

    async def _run() -> Any:
        return await client.generate_text(
            prompt="你好", model="deepseek-chat", stream_enabled=True
        )

    result = asyncio.run(_run())

    assert result.content == "内容"
    assert result.usage is None


def test_chat_stream_mid_error_discards_partial_and_closes(
    monkeypatch: Any,
) -> None:
    """断流/上游报错：丢弃部分聚合结果，抛错进入现有失败链路。"""
    stream = _FakeStream(
        [_text_chunk("部分")],
        error=RuntimeError("上游连接中断"),
    )
    completions = _FakeCompletions(stream=stream)
    client = _make_client(monkeypatch, completions)

    async def _run() -> Any:
        with pytest.raises(RuntimeError, match="上游连接中断"):
            await client.generate_text(
                prompt="你好", model="deepseek-chat", stream_enabled=True
            )

    asyncio.run(_run())
    assert stream.closed is True


def test_chat_stream_cancellation_closes_stream(monkeypatch: Any) -> None:
    """取消路径：CancelledError 原样传播，stream 被关闭。"""

    class _BlockingStream:
        def __init__(self) -> None:
            self.closed = False

        def __aiter__(self) -> Any:
            return self._iterate()

        async def _iterate(self) -> Any:
            yield _text_chunk("部分")
            await asyncio.Event().wait()  # 永不返回
            return
            yield  # pragma: no cover

        async def close(self) -> None:
            self.closed = True

    stream = _BlockingStream()
    completions = _FakeCompletions()
    client = _make_client(monkeypatch, completions)

    async def _create(**kwargs: Any) -> Any:
        completions.last_kwargs = kwargs
        return stream

    async def _run() -> Any:
        monkeypatch.setattr(completions, "create", _create)
        task = asyncio.create_task(
            client.generate_text(
                prompt="你好", model="deepseek-chat", stream_enabled=True
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())
    assert stream.closed is True
