"""HTTP contract tests for the restricted DSH management routes."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi import FastAPI

from komari_bot.agent.management import (
    DshBindingResult,
    DshCredentialResult,
    DshDeleteResult,
    DshPromptResult,
    DshProviderListResult,
    DshProviderResult,
)
from komari_bot.agent.types import AgentControlError
from komari_bot.plugins.komari_management.dsh_api import (
    API_PREFIX,
    register_dsh_management_api,
)

if TYPE_CHECKING:
    from nonebug import App

    from komari_bot.management.management_audit import ManagementAuditEvent

_TOKEN = "secret-token-00000000"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_WRITE_HEADERS = {
    **_AUTH,
    "X-Komari-Change-Reason": "验证 DSH 管理写入",
    "X-Request-ID": "dsh-management-test",
    "If-Match": '"etag-1"',
}
_SECRET = "plain-management-secret-7d31"
_PROMPT = "literal administrator prompt text"
_PROVIDER_URL = "http://127.0.0.1:9/v1"


def _credentials(*, permissions: list[str] | None = None) -> list[dict[str, object]]:
    return [
        {
            "credential_id": "dsh-operator",
            "token": _TOKEN,
            "permissions": permissions or ["*"],
        }
    ]


@dataclass
class _FakeClient:
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)
    fail_with: str | None = None

    def _record(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))
        if self.fail_with is not None:
            raise AgentControlError(self.fail_with)

    async def list_providers(self) -> DshProviderListResult:
        self._record("list_providers")
        return DshProviderListResult(
            items=(
                {
                    "provider": "fixture",
                    "api": "openai-completions",
                    "baseURL": _PROVIDER_URL,
                    "models": [{"id": "fixture-model"}],
                },
            ),
            protocols=("openai-completions", "anthropic-messages"),
            etag="etag-1",
        )

    async def get_provider(self, provider: str) -> DshProviderResult:
        self._record("get_provider", provider)
        return DshProviderResult(
            provider={
                "provider": provider,
                "api": "openai-completions",
                "baseURL": _PROVIDER_URL,
                "models": [{"id": "fixture-model"}],
            },
            etag="etag-1",
        )

    async def put_provider(
        self,
        provider: str,
        profile: Any,
        expected_etag: str,
    ) -> DshProviderResult:
        self._record("put_provider", provider, profile, expected_etag)
        return DshProviderResult(
            provider={"provider": provider, **profile},
            etag="etag-2",
        )

    async def delete_provider(self, provider: str, expected_etag: str) -> DshDeleteResult:
        self._record("delete_provider", provider, expected_etag)
        return DshDeleteResult(etag="etag-2")

    async def get_credential(self, provider: str) -> DshCredentialResult:
        self._record("get_credential", provider)
        return DshCredentialResult(
            provider=provider,
            configured=True,
            writable=True,
            source="file",
            etag="etag-1",
        )

    async def put_credential(
        self,
        provider: str,
        value: str,
        expected_etag: str,
    ) -> DshCredentialResult:
        self._record("put_credential", provider, value, expected_etag)
        return DshCredentialResult(
            provider=provider,
            configured=True,
            writable=True,
            source="file",
            etag="etag-2",
        )

    async def delete_credential(
        self,
        provider: str,
        expected_etag: str,
    ) -> DshCredentialResult:
        self._record("delete_credential", provider, expected_etag)
        return DshCredentialResult(
            provider=provider,
            configured=False,
            writable=True,
            source=None,
            etag="etag-2",
        )

    async def get_binding(self, kind: str) -> DshBindingResult:
        self._record("get_binding", kind)
        return DshBindingResult(
            kind=kind,
            provider="fixture",
            model="fixture-model",
            reasoning_effort=None,
            etag="etag-1",
        )

    async def put_binding(
        self,
        kind: str,
        provider: str,
        model: str,
        reasoning_effort: str | None,
        expected_etag: str,
    ) -> DshBindingResult:
        self._record("put_binding", kind, provider, model, reasoning_effort, expected_etag)
        return DshBindingResult(
            kind=kind,
            provider=provider,
            model=model,
            reasoning_effort=reasoning_effort,
            etag="etag-2",
        )

    async def delete_binding(self, kind: str, expected_etag: str) -> DshBindingResult:
        self._record("delete_binding", kind, expected_etag)
        return DshBindingResult(
            kind=kind,
            provider=None,
            model=None,
            reasoning_effort=None,
            etag="etag-2",
        )

    async def get_prompt(self, kind: str) -> DshPromptResult:
        self._record("get_prompt", kind)
        return DshPromptResult(kind=kind, value=_PROMPT, etag="etag-1")

    async def put_prompt(self, kind: str, value: str, expected_etag: str) -> DshPromptResult:
        self._record("put_prompt", kind, value, expected_etag)
        return DshPromptResult(kind=kind, value=value, etag="etag-2")


def _app(
    client: _FakeClient,
    *,
    permissions: list[str] | None = None,
    audit_events: list[ManagementAuditEvent] | None = None,
) -> FastAPI:
    async def _record(event: ManagementAuditEvent) -> None:
        if audit_events is not None:
            audit_events.append(event)

    app = FastAPI()
    register_dsh_management_api(
        app,
        api_token=_credentials(permissions=permissions),
        allowed_origins=[],
        client_getter=lambda: client,
        audit_recorder=_record,
    )
    return app


def _write_body() -> dict[str, object]:
    return {
        "api": "openai-completions",
        "baseURL": _PROVIDER_URL,
        "models": [{"id": "fixture-model"}],
    }


@pytest.mark.asyncio
async def test_reads_require_token_and_return_strong_etags(app: App) -> None:
    client = _FakeClient()
    async with app.test_server(asgi=cast("Any", _app(client))) as ctx:
        http = ctx.get_client()
        unauthorized = await http.get(f"{API_PREFIX}/providers")
        providers = await http.get(f"{API_PREFIX}/providers", headers=_AUTH)
        provider = await http.get(f"{API_PREFIX}/providers/fixture", headers=_AUTH)
        binding = await http.get(f"{API_PREFIX}/bindings/chat", headers=_AUTH)
        prompt = await http.get(f"{API_PREFIX}/prompts/chat", headers=_AUTH)

    assert unauthorized.status_code == 401
    assert providers.status_code == 200
    assert providers.headers["etag"] == '"etag-1"'
    assert providers.json()["etag"] == "etag-1"
    assert providers.json()["items"][0]["provider"] == "fixture"
    assert provider.status_code == 200
    assert provider.headers["etag"] == '"etag-1"'
    assert provider.json()["etag"] == "etag-1"
    assert binding.status_code == 200
    assert binding.headers["etag"] == '"etag-1"'
    assert "prompt" not in binding.json()
    assert prompt.status_code == 200
    assert prompt.json()["value"] == _PROMPT
    assert prompt.headers["etag"] == '"etag-1"'


@pytest.mark.asyncio
async def test_permission_separation_rejects_cross_resource_access(app: App) -> None:
    client = _FakeClient()
    async with app.test_server(asgi=cast("Any", _app(client, permissions=["prompt:read"]))) as ctx:
        http = ctx.get_client()
        prompt = await http.get(f"{API_PREFIX}/prompts/chat", headers=_AUTH)
        providers = await http.get(f"{API_PREFIX}/providers", headers=_AUTH)
        provider_write = await http.put(
            f"{API_PREFIX}/providers/fixture",
            headers=_WRITE_HEADERS,
            json=_write_body(),
        )

    assert prompt.status_code == 200
    assert providers.status_code == 403
    assert provider_write.status_code == 403
    assert [name for name, _args in client.calls] == ["get_prompt"]


@pytest.mark.asyncio
async def test_unauthorized_writes_are_rejected_without_mutation(app: App) -> None:
    client = _FakeClient()
    async with app.test_server(asgi=cast("Any", _app(client))) as ctx:
        http = ctx.get_client()
        missing_token = await http.put(
            f"{API_PREFIX}/providers/fixture",
            headers={"X-Komari-Change-Reason": "nope", "If-Match": '"etag-1"'},
            json=_write_body(),
        )
        missing_reason = await http.put(
            f"{API_PREFIX}/providers/fixture",
            headers={"Authorization": f"Bearer {_TOKEN}", "If-Match": '"etag-1"'},
            json=_write_body(),
        )
        wrong_token = await http.put(
            f"{API_PREFIX}/providers/fixture",
            headers={
                **_WRITE_HEADERS,
                "Authorization": "Bearer wrong-token-00000000",
            },
            json=_write_body(),
        )

    assert missing_token.status_code == 401
    assert missing_reason.status_code == 400
    assert wrong_token.status_code == 401
    assert client.calls == []


@pytest.mark.asyncio
async def test_write_preconditions_and_safe_error_codes(app: App) -> None:
    client = _FakeClient()
    async with app.test_server(asgi=cast("Any", _app(client))) as ctx:
        http = ctx.get_client()
        missing = await http.put(
            f"{API_PREFIX}/providers/fixture",
            headers=_AUTH | {"X-Komari-Change-Reason": "missing"},
            json=_write_body(),
        )
        weak = await http.put(
            f"{API_PREFIX}/providers/fixture",
            headers={**_WRITE_HEADERS, "If-Match": 'W/"etag-1"'},
            json=_write_body(),
        )
        wildcard = await http.put(
            f"{API_PREFIX}/providers/fixture",
            headers={**_WRITE_HEADERS, "If-Match": "*"},
            json=_write_body(),
        )
        malformed = await http.put(
            f"{API_PREFIX}/providers/fixture",
            headers={**_WRITE_HEADERS, "If-Match": "not a tag"},
            json=_write_body(),
        )

        client.fail_with = "revision_conflict"
        stale = await http.put(
            f"{API_PREFIX}/providers/fixture",
            headers=_WRITE_HEADERS,
            json=_write_body(),
        )
        client.fail_with = "active_tasks"
        active = await http.put(
            f"{API_PREFIX}/providers/fixture",
            headers=_WRITE_HEADERS,
            json=_write_body(),
        )
        client.fail_with = "provider_in_use"
        in_use = await http.delete(
            f"{API_PREFIX}/providers/fixture",
            headers=_WRITE_HEADERS,
        )
        client.fail_with = "unavailable"
        unavailable = await http.put(
            f"{API_PREFIX}/providers/fixture",
            headers=_WRITE_HEADERS,
            json=_write_body(),
        )

    assert missing.status_code == 428
    assert weak.status_code == 422
    assert wildcard.status_code == 422
    assert malformed.status_code == 422
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "revision_conflict"
    assert active.status_code == 409
    assert active.json()["detail"]["code"] == "active_tasks"
    assert in_use.status_code == 409
    assert in_use.json()["detail"]["code"] == "provider_in_use"
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["code"] == "unavailable"


@pytest.mark.asyncio
async def test_credential_validation_and_reads_never_echo_the_secret(app: App) -> None:
    client = _FakeClient()
    audit_events: list[ManagementAuditEvent] = []
    async with app.test_server(
        asgi=cast("Any", _app(client, audit_events=audit_events)),
    ) as ctx:
        http = ctx.get_client()
        read = await http.get(f"{API_PREFIX}/providers/fixture/credential", headers=_AUTH)
        invalid = await http.put(
            f"{API_PREFIX}/providers/fixture/credential",
            headers=_WRITE_HEADERS,
            json={"value": _SECRET, "unknown": "x"},
        )
        saved = await http.put(
            f"{API_PREFIX}/providers/fixture/credential",
            headers=_WRITE_HEADERS,
            json={"value": _SECRET},
        )
        provider = await http.get(f"{API_PREFIX}/providers/fixture", headers=_AUTH)

    for response in (read, invalid, saved, provider):
        assert _SECRET not in response.text
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "invalid_request"
    assert saved.status_code == 200
    assert saved.json()["configured"] is True
    assert audit_events
    assert _SECRET not in json.dumps(
        [event.to_dict() for event in audit_events],
        ensure_ascii=False,
    )
    assert any(event.action == "dsh.credential.put" for event in audit_events)


@pytest.mark.asyncio
async def test_provider_validation_rejects_secret_bearing_urls_and_unknown_fields(
    app: App,
) -> None:
    client = _FakeClient()
    async with app.test_server(asgi=cast("Any", _app(client))) as ctx:
        http = ctx.get_client()
        credential_url = await http.put(
            f"{API_PREFIX}/providers/fixture",
            headers=_WRITE_HEADERS,
            json={
                "api": "openai-completions",
                "baseURL": f"http://operator:{_SECRET}@127.0.0.1:9/v1",
                "models": [{"id": "fixture-model"}],
            },
        )
        unknown = await http.put(
            f"{API_PREFIX}/providers/fixture",
            headers=_WRITE_HEADERS,
            json={**_write_body(), "apiKeyEnv": _SECRET},
        )
        invalid_id = await http.get(
            f"{API_PREFIX}/providers/Bad_ID",
            headers=_AUTH,
        )

    assert credential_url.status_code == 422
    assert unknown.status_code == 422
    assert invalid_id.status_code == 422
    assert _SECRET not in credential_url.text
    assert client.calls == []


@pytest.mark.asyncio
async def test_prompt_write_audits_without_prompt_text(app: App) -> None:
    client = _FakeClient()
    audit_events: list[ManagementAuditEvent] = []
    async with app.test_server(
        asgi=cast("Any", _app(client, audit_events=audit_events)),
    ) as ctx:
        http = ctx.get_client()
        written = await http.put(
            f"{API_PREFIX}/prompts/chat",
            headers=_WRITE_HEADERS,
            json={"value": _PROMPT},
        )

    assert written.status_code == 200
    assert written.json()["value"] == _PROMPT
    assert written.headers["etag"] == '"etag-2"'
    serialized = json.dumps([event.to_dict() for event in audit_events], ensure_ascii=False)
    assert _PROMPT not in serialized
    assert _PROVIDER_URL not in serialized
    assert any(event.action == "dsh.prompt.put" for event in audit_events)
