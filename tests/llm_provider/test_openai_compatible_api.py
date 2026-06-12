"""OpenAI 兼容 API 客户端配置与请求测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from komari_bot.plugins.llm_provider import openai_compatible_api as openai_api_module
from komari_bot.plugins.llm_provider.config import Config
from komari_bot.plugins.llm_provider.config_schema import DynamicConfigSchema
from komari_bot.plugins.llm_provider.openai_compatible_api import OpenAICompatibleClient


def test_llm_provider_timeout_defaults_to_300_seconds() -> None:
    assert DynamicConfigSchema().timeout_seconds == 300.0
    assert Config().timeout_seconds == 300.0
    assert DynamicConfigSchema().reasoning_effort == ""
    assert Config().reasoning_effort == ""
    assert DynamicConfigSchema().extra_params == {}


def test_llm_provider_schema_includes_runtime_fields() -> None:
    config = DynamicConfigSchema()

    assert config.timeout_seconds == 300.0
    assert config.reasoning_effort == ""
    assert config.extra_params == {}


def test_openai_compatible_client_session_uses_configured_timeout() -> None:
    class _FakeAsyncOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def close(self) -> None:
            return None

    async def _run() -> None:
        openai_api_module.AsyncOpenAI = _FakeAsyncOpenAI  # type: ignore[method-assign]
        client = OpenAICompatibleClient(
            "token",
            base_url="https://example.com/v1",
            timeout_seconds=300.0,
        )
        try:
            sdk_client = client.client
            assert isinstance(sdk_client, _FakeAsyncOpenAI)
            assert sdk_client.kwargs["timeout"] == 300.0
            assert sdk_client.kwargs["base_url"] == "https://example.com/v1"
        finally:
            await client.close()

    asyncio.run(_run())


def test_openai_compatible_client_generate_text_includes_reasoning_effort(
    monkeypatch: Any,
) -> None:
    class _FakeCompletions:
        def __init__(self) -> None:
            self.last_kwargs: dict[str, Any] | None = None

        async def create(self, **kwargs: Any) -> Any:
            self.last_kwargs = kwargs
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
            )

    class _FakeClient:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(completions=_FakeCompletions())

        async def close(self) -> None:
            return None

    async def _run() -> None:
        client = OpenAICompatibleClient(
            "token",
            base_url="https://example.com/v1",
            timeout_seconds=300.0,
        )
        fake_client = _FakeClient()
        monkeypatch.setattr(client, "client", fake_client)
        monkeypatch.setattr(
            openai_api_module,
            "config_manager",
            SimpleNamespace(
                get=lambda: SimpleNamespace(
                    temperature=1.0,
                    max_tokens=8192,
                    frequency_penalty=0.0,
                    api_base="https://example.com/v1",
                    reasoning_effort="medium",
                )
            ),
        )

        result = await client.generate_text(prompt="你好", model="deepseek-chat")

        assert result.content == "ok"
        request_data = fake_client.chat.completions.last_kwargs
        assert request_data is not None
        assert request_data["reasoning_effort"] == "medium"
        assert "response_format" not in request_data

    asyncio.run(_run())


def test_openai_compatible_client_generate_text_passes_response_format(
    monkeypatch: Any,
) -> None:
    class _FakeCompletions:
        def __init__(self) -> None:
            self.last_kwargs: dict[str, Any] | None = None

        async def create(self, **kwargs: Any) -> Any:
            self.last_kwargs = kwargs
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
            )

    class _FakeClient:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(completions=_FakeCompletions())

        async def close(self) -> None:
            return None

    async def _run() -> None:
        client = OpenAICompatibleClient(
            "token",
            base_url="https://example.com/v1",
            timeout_seconds=300.0,
        )
        fake_client = _FakeClient()
        monkeypatch.setattr(client, "client", fake_client)
        monkeypatch.setattr(
            openai_api_module,
            "config_manager",
            SimpleNamespace(
                get=lambda: SimpleNamespace(
                    temperature=1.0,
                    max_tokens=8192,
                    frequency_penalty=0.0,
                    api_base="https://example.com/v1",
                    reasoning_effort="",
                )
            ),
        )

        result = await client.generate_text(
            prompt="请返回 JSON，对象字段为 name 和 age",
            model="deepseek-chat",
            response_format={"type": "json_object"},
        )

        assert result.content == "ok"
        request_data = fake_client.chat.completions.last_kwargs
        assert request_data is not None
        assert request_data["response_format"] == {"type": "json_object"}

    asyncio.run(_run())


def test_openai_compatible_client_generate_text_with_messages_passes_response_format(
    monkeypatch: Any,
) -> None:
    class _FakeCompletions:
        def __init__(self) -> None:
            self.last_kwargs: dict[str, Any] | None = None

        async def create(self, **kwargs: Any) -> Any:
            self.last_kwargs = kwargs
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
            )

    class _FakeClient:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(completions=_FakeCompletions())

        async def close(self) -> None:
            return None

    async def _run() -> None:
        client = OpenAICompatibleClient(
            "token",
            base_url="https://example.com/v1",
            timeout_seconds=300.0,
        )
        fake_client = _FakeClient()
        monkeypatch.setattr(client, "client", fake_client)
        monkeypatch.setattr(
            openai_api_module,
            "config_manager",
            SimpleNamespace(
                get=lambda: SimpleNamespace(
                    temperature=1.0,
                    max_tokens=8192,
                    frequency_penalty=0.0,
                    api_base="https://example.com/v1",
                    reasoning_effort="",
                )
            ),
        )

        result = await client.generate_text_with_messages(
            messages=[{"role": "user", "content": "请返回 JSON"}],
            model="deepseek-chat",
            response_format={"type": "json_object"},
        )

        assert result.content == "ok"
        request_data = fake_client.chat.completions.last_kwargs
        assert request_data is not None
        assert request_data["response_format"] == {"type": "json_object"}

    asyncio.run(_run())


def test_openai_compatible_client_generate_messages_completion_parses_tool_calls(
    monkeypatch: Any,
) -> None:
    class _FakeToolCall:
        def __init__(self) -> None:
            self.id = "call_1"
            self.type = "function"
            self.function = SimpleNamespace(
                name="fetch_recent_group_messages",
                arguments='{"count": 50}',
            )

    class _FakeCompletions:
        async def create(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=None, tool_calls=[_FakeToolCall()]
                        ),
                        finish_reason="tool_calls",
                    )
                ]
            )

    class _FakeClient:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(completions=_FakeCompletions())

        async def close(self) -> None:
            return None

    async def _run() -> None:
        client = OpenAICompatibleClient(
            "token",
            base_url="https://example.com/v1",
            timeout_seconds=300.0,
        )
        monkeypatch.setattr(client, "client", _FakeClient())
        monkeypatch.setattr(
            openai_api_module,
            "config_manager",
            SimpleNamespace(
                get=lambda: SimpleNamespace(
                    temperature=1.0,
                    max_tokens=8192,
                    frequency_penalty=0.0,
                    api_base="https://example.com/v1",
                    reasoning_effort="",
                )
            ),
        )

        result = await client.generate_text_with_messages(
            messages=[{"role": "user", "content": "总结最近 50 条"}],
            model="deepseek-chat",
            tools=[
                {
                    "type": "function",
                    "function": {"name": "fetch_recent_group_messages"},
                }
            ],
            tool_choice="auto",
        )

        assert result.content == ""
        assert result.finish_reason == "tool_calls"
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].function.name == "fetch_recent_group_messages"
        assert result.tool_calls[0].parsed_arguments == {"count": 50}

    asyncio.run(_run())


def test_openai_compatible_client_generate_messages_completion_keeps_invalid_json_arguments(
    monkeypatch: Any,
) -> None:
    class _FakeToolCall:
        def __init__(self) -> None:
            self.id = "call_1"
            self.type = "function"
            self.function = SimpleNamespace(
                name="fetch_messages_by_topic",
                arguments='{"keywords": [发布,]}',
            )

    class _FakeCompletions:
        async def create(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=None, tool_calls=[_FakeToolCall()]
                        ),
                        finish_reason="tool_calls",
                    )
                ]
            )

    class _FakeClient:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(completions=_FakeCompletions())

        async def close(self) -> None:
            return None

    async def _run() -> None:
        client = OpenAICompatibleClient(
            "token",
            base_url="https://example.com/v1",
            timeout_seconds=300.0,
        )
        monkeypatch.setattr(client, "client", _FakeClient())
        monkeypatch.setattr(
            openai_api_module,
            "config_manager",
            SimpleNamespace(
                get=lambda: SimpleNamespace(
                    temperature=1.0,
                    max_tokens=8192,
                    frequency_penalty=0.0,
                    api_base="https://example.com/v1",
                    reasoning_effort="",
                )
            ),
        )

        result = await client.generate_text_with_messages(
            messages=[{"role": "user", "content": "总结最近关于发布的讨论"}],
            model="deepseek-chat",
        )

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].raw_arguments == '{"keywords": [发布,]}'
        assert result.tool_calls[0].parsed_arguments is None

    asyncio.run(_run())


def test_openai_compatible_client_generate_text_sends_extra_params_via_extra_body(
    monkeypatch: Any,
) -> None:
    class _FakeCompletions:
        def __init__(self) -> None:
            self.last_kwargs: dict[str, Any] | None = None

        async def create(self, **kwargs: Any) -> Any:
            self.last_kwargs = kwargs
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
            )

    class _FakeClient:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(completions=_FakeCompletions())

        async def close(self) -> None:
            return None

    async def _run() -> None:
        client = OpenAICompatibleClient(
            "token",
            base_url="https://example.com/v1",
            timeout_seconds=300.0,
        )
        fake_client = _FakeClient()
        monkeypatch.setattr(client, "client", fake_client)
        monkeypatch.setattr(
            openai_api_module,
            "config_manager",
            SimpleNamespace(
                get=lambda: SimpleNamespace(
                    temperature=1.0,
                    max_tokens=8192,
                    frequency_penalty=0.0,
                    api_base="https://example.com/v1",
                    reasoning_effort="",
                    extra_params={
                        "enable_thinking": False,
                        "thinking": {"type": "disabled"},
                    },
                )
            ),
        )

        result = await client.generate_text(prompt="你好", model="deepseek-chat")

        assert result.content == "ok"
        request_data = fake_client.chat.completions.last_kwargs
        assert request_data is not None
        assert request_data["extra_body"] == {
            "enable_thinking": False,
            "thinking": {"type": "disabled"},
        }
        assert "enable_thinking" not in request_data

    asyncio.run(_run())


def test_openai_compatible_client_generate_text_debug_log_uses_statistics(
    monkeypatch: Any,
) -> None:
    class _FakeCompletions:
        async def create(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
            )

    class _FakeClient:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(completions=_FakeCompletions())

        async def close(self) -> None:
            return None

    debug_messages: list[str] = []

    def _fake_debug(message: str, *args: Any) -> None:
        debug_messages.append(message.format(*args) if args else message)

    async def _run() -> None:
        client = OpenAICompatibleClient(
            "token",
            base_url="https://example.com/v1",
            timeout_seconds=300.0,
        )
        monkeypatch.setattr(client, "client", _FakeClient())
        monkeypatch.setattr(openai_api_module.logger, "debug", _fake_debug)
        monkeypatch.setattr(
            openai_api_module,
            "config_manager",
            SimpleNamespace(
                get=lambda: SimpleNamespace(
                    temperature=1.0,
                    max_tokens=8192,
                    frequency_penalty=0.0,
                    api_base="https://example.com/v1",
                    reasoning_effort="medium",
                    extra_params={},
                )
            ),
        )

        await client.generate_text(
            prompt="绝密 prompt 原文",
            model="deepseek-chat",
            system_instruction="绝密 system 原文",
            response_format={"type": "json_object"},
            tools=[{"type": "function", "function": {"name": "query"}}],
        )

    asyncio.run(_run())

    request_log = next(message for message in debug_messages if "OpenAI 兼容 API 请求" in message)
    assert "prompt_chars: 12" in request_log
    assert "system_instruction_chars: 12" in request_log
    assert "tools_count: 1" in request_log
    assert "reasoning_effort: medium" in request_log
    assert "has_response_format: True" in request_log
    assert "绝密 prompt 原文" not in request_log
    assert "绝密 system 原文" not in request_log


def test_openai_compatible_client_generate_messages_debug_log_includes_request_flags(
    monkeypatch: Any,
) -> None:
    class _FakeCompletions:
        async def create(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
            )

    class _FakeClient:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(completions=_FakeCompletions())

        async def close(self) -> None:
            return None

    debug_messages: list[str] = []

    def _fake_debug(message: str, *args: Any) -> None:
        debug_messages.append(message.format(*args) if args else message)

    async def _run() -> None:
        client = OpenAICompatibleClient(
            "token",
            base_url="https://example.com/v1",
            timeout_seconds=300.0,
        )
        monkeypatch.setattr(client, "client", _FakeClient())
        monkeypatch.setattr(openai_api_module.logger, "debug", _fake_debug)
        monkeypatch.setattr(
            openai_api_module,
            "config_manager",
            SimpleNamespace(
                get=lambda: SimpleNamespace(
                    temperature=1.0,
                    max_tokens=8192,
                    frequency_penalty=0.1,
                    api_base="https://example.com/v1",
                    reasoning_effort="low",
                    extra_params={},
                )
            ),
        )

        await client.generate_text_with_messages(
            messages=[{"role": "user", "content": "你好"}],
            model="deepseek-chat",
            response_format={"type": "json_object"},
            tools=[{"type": "function", "function": {"name": "query"}}],
            parallel_tool_calls=False,
        )

    asyncio.run(_run())

    request_log = next(
        message for message in debug_messages if "OpenAI 兼容 API 请求 (messages)" in message
    )
    assert "tools_count: 1" in request_log
    assert "has_response_format: True" in request_log
    assert "has_parallel_tool_calls: True" in request_log
    assert "frequency_penalty: 0.1" in request_log
