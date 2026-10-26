"""Typed management facade over the private DSH control pipe.

The HTTP management plugin depends only on this narrow surface; it never sees
task execution, callback registration, or child-process details.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

from .types import AgentControlError, AgentControlResult, ControlOperation, JsonValue

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

class ControlHost(Protocol):
    """The one host capability this facade needs."""

    async def control(
        self,
        operation: ControlOperation,
        args: Mapping[str, JsonValue],
        *,
        wait_seconds: float | None = None,
    ) -> AgentControlResult: ...


@dataclass(frozen=True, slots=True)
class DshProviderListResult:
    """Configured provider views plus the protocol identifiers the adapter serves."""

    items: tuple[Mapping[str, JsonValue], ...]
    protocols: tuple[str, ...]
    etag: str


@dataclass(frozen=True, slots=True)
class DshProviderResult:
    """One redacted provider definition."""

    provider: Mapping[str, JsonValue]
    etag: str


@dataclass(frozen=True, slots=True)
class DshCredentialResult:
    """Presence and writability facts for one credential reference."""

    provider: str
    configured: bool
    writable: bool
    source: str | None
    etag: str


@dataclass(frozen=True, slots=True)
class DshBindingResult:
    """One model selection without its prompt text."""

    kind: str
    provider: str | None
    model: str | None
    reasoning_effort: str | None
    etag: str


@dataclass(frozen=True, slots=True)
class DshPromptResult:
    """One literal long-term preset prompt."""

    kind: str
    value: str
    etag: str


@dataclass(frozen=True, slots=True)
class DshDeleteResult:
    """One successful deletion and the resource collection's new ETag."""

    etag: str


def _mapping(value: object, label: str) -> Mapping[str, JsonValue]:
    if not isinstance(value, dict):
        raise AgentControlError("unavailable")
    _ = label
    return cast("Mapping[str, JsonValue]", value)


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise AgentControlError("unavailable")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return _string(value)


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise AgentControlError("unavailable")
    return value


class DshManagementClient:
    """Restricted management access over the existing AgentHost transport."""

    def __init__(self, host_getter: Callable[[], ControlHost]) -> None:
        self._host_getter = host_getter

    def _host(self) -> ControlHost:
        return self._host_getter()

    async def _call(
        self,
        operation: ControlOperation,
        args: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]:
        result = await self._host().control(operation, args)
        return result.value

    async def list_providers(self) -> DshProviderListResult:
        value = await self._call(ControlOperation.PROVIDERS_LIST, {})
        raw_items = value.get("items")
        raw_protocols = value.get("protocols")
        if not isinstance(raw_items, list) or not isinstance(raw_protocols, list):
            raise AgentControlError("unavailable")
        items = tuple(_mapping(item, "provider") for item in raw_items)
        protocols = tuple(_string(item) for item in raw_protocols)
        return DshProviderListResult(
            items=items,
            protocols=protocols,
            etag=_string(value.get("etag")),
        )

    async def get_provider(self, provider: str) -> DshProviderResult:
        value = await self._call(ControlOperation.PROVIDERS_GET, {"provider": provider})
        return DshProviderResult(
            provider=_mapping(value.get("provider"), "provider"),
            etag=_string(value.get("etag")),
        )

    async def put_provider(
        self,
        provider: str,
        profile: Mapping[str, JsonValue],
        expected_etag: str,
    ) -> DshProviderResult:
        value = await self._call(
            ControlOperation.PROVIDERS_PUT,
            {
                "provider": provider,
                "profile": dict(profile),
                "expectedEtag": expected_etag,
            },
        )
        return DshProviderResult(
            provider=_mapping(value.get("provider"), "provider"),
            etag=_string(value.get("etag")),
        )

    async def delete_provider(self, provider: str, expected_etag: str) -> DshDeleteResult:
        value = await self._call(
            ControlOperation.PROVIDERS_DELETE,
            {"provider": provider, "expectedEtag": expected_etag},
        )
        return DshDeleteResult(etag=_string(value.get("etag")))

    async def get_credential(self, provider: str) -> DshCredentialResult:
        value = await self._call(ControlOperation.CREDENTIAL_GET, {"provider": provider})
        return self._credential_result(value)

    async def put_credential(
        self,
        provider: str,
        value: str,
        expected_etag: str,
    ) -> DshCredentialResult:
        result = await self._call(
            ControlOperation.CREDENTIAL_PUT,
            {"provider": provider, "value": value, "expectedEtag": expected_etag},
        )
        return self._credential_result(result)

    async def delete_credential(self, provider: str, expected_etag: str) -> DshCredentialResult:
        value = await self._call(
            ControlOperation.CREDENTIAL_DELETE,
            {"provider": provider, "expectedEtag": expected_etag},
        )
        return self._credential_result(value)

    @staticmethod
    def _credential_result(value: Mapping[str, JsonValue]) -> DshCredentialResult:
        credential = _mapping(value.get("credential"), "credential")
        return DshCredentialResult(
            provider=_string(credential.get("provider")),
            configured=_bool(credential.get("configured")),
            writable=_bool(credential.get("writable")),
            source=_optional_string(credential.get("source")),
            etag=_string(value.get("etag")),
        )

    async def get_binding(self, kind: str) -> DshBindingResult:
        value = await self._call(ControlOperation.BINDING_GET, {"kind": kind})
        return self._binding_result(value)

    async def put_binding(
        self,
        kind: str,
        provider: str,
        model: str,
        reasoning_effort: str | None,
        expected_etag: str,
    ) -> DshBindingResult:
        args: dict[str, JsonValue] = {
            "kind": kind,
            "provider": provider,
            "model": model,
            "expectedEtag": expected_etag,
        }
        if reasoning_effort is not None:
            args["reasoningEffort"] = reasoning_effort
        value = await self._call(ControlOperation.BINDING_PUT, args)
        return self._binding_result(value)

    async def delete_binding(self, kind: str, expected_etag: str) -> DshBindingResult:
        value = await self._call(
            ControlOperation.BINDING_DELETE,
            {"kind": kind, "expectedEtag": expected_etag},
        )
        return self._binding_result(value)

    @staticmethod
    def _binding_result(value: Mapping[str, JsonValue]) -> DshBindingResult:
        binding = _mapping(value.get("binding"), "binding")
        return DshBindingResult(
            kind=_string(binding.get("kind")),
            provider=_optional_string(binding.get("provider")),
            model=_optional_string(binding.get("model")),
            reasoning_effort=_optional_string(binding.get("reasoningEffort")),
            etag=_string(value.get("etag")),
        )

    async def get_prompt(self, kind: str) -> DshPromptResult:
        value = await self._call(ControlOperation.PROMPT_GET, {"kind": kind})
        return self._prompt_result(value)

    async def put_prompt(
        self,
        kind: str,
        value: str,
        expected_etag: str,
    ) -> DshPromptResult:
        result = await self._call(
            ControlOperation.PROMPT_PUT,
            {"kind": kind, "value": value, "expectedEtag": expected_etag},
        )
        return self._prompt_result(result)

    @staticmethod
    def _prompt_result(value: Mapping[str, JsonValue]) -> DshPromptResult:
        prompt = _mapping(value.get("prompt"), "prompt")
        return DshPromptResult(
            kind=_string(prompt.get("kind")),
            value=_string(prompt.get("value")),
            etag=_string(value.get("etag")),
        )


__all__ = [
    "ControlHost",
    "DshBindingResult",
    "DshCredentialResult",
    "DshDeleteResult",
    "DshManagementClient",
    "DshPromptResult",
    "DshProviderListResult",
    "DshProviderResult",
]
