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
    async def _run() -> None:
        client = DeepSeekClient("token", timeout_seconds=300.0)
        session = await client._get_session()
        try:
            assert session.timeout.total == 300.0
        finally:
            await client.close()

    asyncio.run(_run())


def test_deepseek_client_generate_text_includes_reasoning_effort(
    monkeypatch: Any,
) -> None:
    class _FakeResponse:
        status = 200

        async def json(self) -> dict[str, Any]:
            return {"choices": [{"message": {"content": "ok"}}]}

    class _FakePostContext:
        def __init__(self, response: _FakeResponse) -> None:
            self._response = response

        async def __aenter__(self) -> _FakeResponse:
            return self._response

        async def __aexit__(
            self,
            exc_type: object,
            exc: object,
            tb: object,
        ) -> None:
            del exc_type, exc, tb

    class _FakeSession:
        def __init__(self) -> None:
            self.last_json: dict[str, Any] | None = None

        def post(self, _url: str, *, json: dict[str, Any]) -> _FakePostContext:
            self.last_json = json
            return _FakePostContext(_FakeResponse())

    async def _run() -> None:
        fake_session = _FakeSession()
        client = DeepSeekClient("token", timeout_seconds=300.0)

        async def _fake_get_session() -> _FakeSession:
            return fake_session

        monkeypatch.setattr(client, "_get_session", _fake_get_session)
        monkeypatch.setattr(
            deepseek_client_module,
            "config_manager",
            SimpleNamespace(
                get=lambda: SimpleNamespace(
                    deepseek_temperature=1.0,
                    deepseek_max_tokens=8192,
                    deepseek_frequency_penalty=0.0,
                    deepseek_api_base="https://example.com/v1/chat/completions",
                    deepseek_reasoning_effort="medium",
                )
            ),
        )

        result = await client.generate_text(prompt="你好", model="deepseek-chat")

        assert result == "ok"
        assert fake_session.last_json is not None
        assert fake_session.last_json["reasoning_effort"] == "medium"

    asyncio.run(_run())
