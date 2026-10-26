"""Real-DSH control-plane transport tests for TSK-202."""

from __future__ import annotations

from typing import TYPE_CHECKING

from komari_bot.agent import (
    ControlOperation,
    ReadinessState,
    TaskKind,
)
from tests.komari_agent.dsh_test_support import host_for

if TYPE_CHECKING:
    from pathlib import Path


async def test_empty_profile_control_round_trip_updates_readiness(tmp_path: Path) -> None:
    host = host_for(tmp_path / "data")
    await host.start()
    try:
        listed = await host.control(ControlOperation.PROVIDERS_LIST, {})
        assert listed.value["items"] == []
        protocols = listed.value["protocols"]
        assert isinstance(protocols, list)
        assert "openai-completions" in protocols
        assert isinstance(listed.value["etag"], str)

        created = await host.control(
            ControlOperation.PROVIDERS_PUT,
            {
                "provider": "fixture",
                "expectedEtag": listed.value["etag"],
                "profile": {
                    "api": "openai-completions",
                    "baseURL": "http://127.0.0.1:9/v1",
                    "models": [{"id": "fixture-model"}],
                },
            },
        )
        provider = created.value["provider"]
        assert isinstance(provider, dict)
        assert provider["provider"] == "fixture"
        assert "apiKeyEnv" not in provider

        credential = await host.control(
            ControlOperation.CREDENTIAL_PUT,
            {
                "provider": "fixture",
                "value": "control-secret-6c2b",
                "expectedEtag": (await host.control(
                    ControlOperation.CREDENTIAL_GET,
                    {"provider": "fixture"},
                )).value["etag"],
            },
        )
        facts = credential.value["credential"]
        assert isinstance(facts, dict)
        assert facts["configured"] is True
        assert "control-secret-6c2b" not in str(credential.value)

        binding = await host.control(
            ControlOperation.BINDING_GET,
            {"kind": "chat"},
        )
        await host.control(
            ControlOperation.BINDING_PUT,
            {
                "kind": "chat",
                "provider": "fixture",
                "model": "fixture-model",
                "expectedEtag": binding.value["etag"],
            },
        )
        prompt = await host.control(ControlOperation.PROMPT_GET, {"kind": "chat"})
        updated = await host.control(
            ControlOperation.PROMPT_PUT,
            {
                "kind": "chat",
                "value": "literal control prompt",
                "expectedEtag": prompt.value["etag"],
            },
        )
        assert updated.value["prompt"] == {"kind": "chat", "value": "literal control prompt"}

        # A successful control result carries the current binding status, which
        # must replace the startup snapshot on the Python host.
        assert host.readiness.state is ReadinessState.UNCONFIGURED_READY
        assert host.readiness.bindings[TaskKind.CHAT] is True
        assert host.readiness.bindings[TaskKind.SUMMARY] is False
    finally:
        await host.close()
