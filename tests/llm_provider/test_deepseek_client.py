"""LLM provider timeout configuration tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

from komari_bot.plugins.llm_provider.config import Config
from komari_bot.plugins.llm_provider.config_schema import DynamicConfigSchema
from komari_bot.plugins.llm_provider.deepseek_client import DeepSeekClient


def test_llm_provider_timeout_defaults_to_300_seconds() -> None:
    assert DynamicConfigSchema().deepseek_timeout_seconds == 300.0
    assert Config().deepseek_timeout_seconds == 300.0


def test_llm_provider_example_includes_timeout_field() -> None:
    example_path = (
        Path(__file__).resolve().parents[2]
        / "config/config_manager/llm_provider_config.json.example"
    )
    content = example_path.read_text(encoding="utf-8")

    assert '"deepseek_timeout_seconds": 300.0' in content


def test_deepseek_client_session_uses_configured_timeout() -> None:
    async def _run() -> None:
        client = DeepSeekClient("token", timeout_seconds=300.0)
        session = await client._get_session()
        try:
            assert session.timeout.total == 300.0
        finally:
            await client.close()

    asyncio.run(_run())
