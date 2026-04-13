"""LLM provider timeout configuration tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from komari_bot.plugins.llm_provider import deepseek_client as deepseek_client_module
from komari_bot.plugins.llm_provider.config import Config
from komari_bot.plugins.llm_provider.config_schema import DynamicConfigSchema
from komari_bot.plugins.llm_provider.deepseek_client import DeepSeekClient


def test_llm_provider_timeout_defaults_to_300_seconds() -> None:
    assert DynamicConfigSchema().deepseek_timeout_seconds == 300.0
    assert Config().deepseek_timeout_seconds == 300.0
    assert DynamicConfigSchema().deepseek_reasoning_effort == ""
    assert Config().deepseek_reasoning_effort == ""


def test_llm_provider_example_includes_timeout_field() -> None:
    example_path = (
        Path(__file__).resolve().parents[2]
        / "config/config_manager/llm_provider_config.json.example"
    )
    content = example_path.read_text(encoding="utf-8")

    assert '"deepseek_timeout_seconds": 300.0' in content
    assert '"deepseek_reasoning_effort": "medium"' in content


def test_deepseek_client_session_uses_configured_timeout() -> None:
    class _FakeAsyncOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def close(self) -> None:
            return None

    async def _run() -> None:
        deepseek_client_module.AsyncOpenAI = _FakeAsyncOpenAI  # type: ignore[method-assign]
        client = DeepSeekClient(
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


def test_deepseek_client_generate_text_includes_reasoning_effort(
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
        client = DeepSeekClient(
            "token",
            base_url="https://example.com/v1",
            timeout_seconds=300.0,
        )
        fake_client = _FakeClient()
        monkeypatch.setattr(client, "client", fake_client)
        monkeypatch.setattr(
            deepseek_client_module,
            "config_manager",
            SimpleNamespace(
                get=lambda: SimpleNamespace(
                    deepseek_temperature=1.0,
                    deepseek_max_tokens=8192,
                    deepseek_frequency_penalty=0.0,
                    deepseek_api_base="https://example.com/v1",
                    deepseek_reasoning_effort="medium",
                )
            ),
        )

        result = await client.generate_text(prompt="你好", model="deepseek-chat")

        assert result == "ok"
        request_data = fake_client.chat.completions.last_kwargs
        assert request_data is not None
        assert request_data["reasoning_effort"] == "medium"
        assert "response_format" not in request_data

    asyncio.run(_run())


def test_deepseek_client_generate_text_ignores_response_format(
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
        client = DeepSeekClient(
            "token",
            base_url="https://example.com/v1",
            timeout_seconds=300.0,
        )
        fake_client = _FakeClient()
        monkeypatch.setattr(client, "client", fake_client)
        monkeypatch.setattr(
            deepseek_client_module,
            "config_manager",
            SimpleNamespace(
                get=lambda: SimpleNamespace(
                    deepseek_temperature=1.0,
                    deepseek_max_tokens=8192,
                    deepseek_frequency_penalty=0.0,
                    deepseek_api_base="https://example.com/v1",
                    deepseek_reasoning_effort="",
                )
            ),
        )

        result = await client.generate_text(
            prompt="请返回 JSON，对象字段为 name 和 age",
            model="deepseek-chat",
            response_format={"type": "json_object"},
        )

        assert result == "ok"
        request_data = fake_client.chat.completions.last_kwargs
        assert request_data is not None
        assert "response_format" not in request_data

    asyncio.run(_run())
