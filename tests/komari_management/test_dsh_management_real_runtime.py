"""End-to-end DSH management tests over the authenticated FastAPI seam."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi import FastAPI

from komari_bot.agent import (
    AgentCallbackHandler,
    AgentCallbackRequest,
    AgentTaskBudget,
    AgentTaskRequest,
    JsonValue,
    ReadinessState,
    TaskKind,
)
from komari_bot.agent.management import DshManagementClient
from komari_bot.plugins.komari_management.dsh_api import (
    API_PREFIX,
    register_dsh_management_api,
)
from tests.komari_agent.dsh_test_support import (
    ScriptedModelServer,
    ToolCallResponse,
    chat_task_data,
    host_for,
)

if TYPE_CHECKING:
    from pathlib import Path

    from nonebug import App

    from komari_bot.management.management_audit import ManagementAuditEvent

_TOKEN = "real-runtime-token-00000000"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_SECRET = "real-runtime-secret-c41e"
_CHAT_PROMPT = "\n  你是一名长期角色管理员文案，逐字保留 {{operator_literal}}。  \n"
_SUMMARY_PROMPT = "独立的总结预设长期指令。"


def _credentials() -> list[dict[str, object]]:
    return [
        {
            "credential_id": "real-runtime-operator",
            "token": _TOKEN,
            "permissions": ["*"],
        }
    ]


def _app(
    host: Any,
    *,
    audit_events: list[ManagementAuditEvent] | None = None,
) -> FastAPI:
    async def _record(event: ManagementAuditEvent) -> None:
        if audit_events is not None:
            audit_events.append(event)

    app = FastAPI()
    register_dsh_management_api(
        app,
        api_token=_credentials(),
        allowed_origins=[],
        client_getter=lambda: DshManagementClient(lambda: host),
        audit_recorder=_record,
    )
    return app


def _write_headers(etag: str, request_id: str) -> dict[str, str]:
    return {
        **_AUTH,
        "X-Komari-Change-Reason": "验收 DSH 管理写入",
        "X-Request-ID": request_id,
        "If-Match": f'"{etag}"',
    }


def _provider_body(
    base_url: str,
    models: tuple[str, ...] = ("fixture-model",),
) -> dict[str, Any]:
    return {
        "api": "openai-completions",
        "baseURL": base_url,
        "displayName": "Controlled local provider",
        "models": [
            {
                "id": model,
                "name": f"Fixture {model}",
                "contextWindow": 8192,
                "maxTokens": 128,
                "input": ["text"],
            }
            for model in models
        ],
        "defaultInput": ["text"],
        "timeoutMs": 2_000,
        "streamIdleTimeoutMs": 2_000,
    }


def _task(task_id: str = "management-task") -> AgentTaskRequest:
    return AgentTaskRequest(
        task_id=task_id,
        kind=TaskKind.CHAT,
        task_data=chat_task_data("manage me"),
        budget=AgentTaskBudget(
            max_rounds=3,
            max_tool_calls_per_round=4,
            max_total_tool_calls=8,
        ),
    )


def _callbacks(
    *,
    admission: AgentCallbackHandler | None = None,
) -> dict[str, AgentCallbackHandler]:
    async def default_admission(request: AgentCallbackRequest) -> JsonValue:
        _ = request
        return {"allow": True}

    async def fixture_result(request: AgentCallbackRequest) -> JsonValue:
        return {"ok": True, "value": request.data.get("value")}

    return {
        "admission": admission or default_admission,
        "fixture_result": fixture_result,
    }


async def _configure_model(
    http: Any,
    base_url: str,
    *,
    models: tuple[str, ...] = ("fixture-model",),
    chat_model: str = "fixture-model",
    chat_prompt: str,
    summary_prompt: str,
    credential: str = _SECRET,
    configure_summary: bool = True,
) -> dict[str, str]:
    providers = await http.get(f"{API_PREFIX}/providers", headers=_AUTH)
    assert providers.status_code == 200
    created = await http.put(
        f"{API_PREFIX}/providers/fixture",
        headers=_write_headers(providers.json()["etag"], "provider-create"),
        json=_provider_body(base_url, models=models),
    )
    assert created.status_code == 200, created.text
    credential_get = await http.get(
        f"{API_PREFIX}/providers/fixture/credential",
        headers=_AUTH,
    )
    assert credential_get.status_code == 200
    saved_credential = await http.put(
        f"{API_PREFIX}/providers/fixture/credential",
        headers=_write_headers(credential_get.json()["etag"], "credential-save"),
        json={"value": credential},
    )
    assert saved_credential.status_code == 200, saved_credential.text
    assert credential not in saved_credential.text

    kinds = ("chat", "summary") if configure_summary else ("chat",)
    for kind in kinds:
        binding_get = await http.get(f"{API_PREFIX}/bindings/{kind}", headers=_AUTH)
        assert binding_get.status_code == 200
        saved_binding = await http.put(
            f"{API_PREFIX}/bindings/{kind}",
            headers=_write_headers(binding_get.json()["etag"], f"binding-{kind}"),
            json={
                "provider": "fixture",
                "model": chat_model if kind == "chat" else "fixture-model",
            },
        )
        assert saved_binding.status_code == 200, saved_binding.text
        prompt_get = await http.get(f"{API_PREFIX}/prompts/{kind}", headers=_AUTH)
        assert prompt_get.status_code == 200
        assert prompt_get.json()["value"] == ""
        prompt = chat_prompt if kind == "chat" else summary_prompt
        saved_prompt = await http.put(
            f"{API_PREFIX}/prompts/{kind}",
            headers=_write_headers(prompt_get.json()["etag"], f"prompt-{kind}"),
            json={"value": prompt},
        )
        assert saved_prompt.status_code == 200, saved_prompt.text
        assert saved_prompt.json()["value"] == prompt
        assert saved_prompt.headers["etag"] == f'"{saved_prompt.json()["etag"]}"'
    return {"provider_etag": created.json()["etag"]}


def _authorization(request: Any) -> str:
    for key, value in request.headers.items():
        if key.lower() == "authorization":
            return value
    return ""


async def test_empty_profile_is_configured_only_through_authenticated_api(
    app: App,
    tmp_path: Path,
) -> None:
    server = ScriptedModelServer()
    server.start()
    server.enqueue(
        ToolCallResponse("fixture_complete", {"value": "configured-through-api"}),
    )
    host = host_for(tmp_path / "data")
    audit_events: list[ManagementAuditEvent] = []
    log_records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            log_records.append(record.getMessage())

    capture = _Capture()
    logging.getLogger("komari_agent.host").addHandler(capture)
    await host.start()
    try:
        api_app = _app(host, audit_events=audit_events)
        async with app.test_server(asgi=cast("Any", api_app)) as ctx:
            http = ctx.get_client()
            unauthorized = await http.get(f"{API_PREFIX}/providers")
            assert unauthorized.status_code == 401

            providers = await http.get(f"{API_PREFIX}/providers", headers=_AUTH)
            assert providers.status_code == 200
            assert providers.json()["items"] == []
            assert "openai-completions" in providers.json()["protocols"]

            # A legacy export is operator input, never an implicit import source.
            legacy_snapshot = {
                "system_prompt": _CHAT_PROMPT,
                "output_instruction": "retired output protocol",
                "delegated_vision": {"enabled": True},
                "revision": 7,
            }
            before = await http.get(f"{API_PREFIX}/prompts/chat", headers=_AUTH)
            assert before.json()["value"] == ""
            assert legacy_snapshot["output_instruction"] not in before.text

            await _configure_model(
                http,
                server.base_url,
                chat_prompt=legacy_snapshot["system_prompt"],
                summary_prompt=_SUMMARY_PROMPT,
            )
            assert host.readiness.state is ReadinessState.READY

            chat_request_count = len(server.requests)
            result = await host.execute(_task(), _callbacks())
            assert result.result == {"ok": True, "value": "configured-through-api"}
            assert len(server.requests) == chat_request_count + 1
            request = server.requests[-1]
            assert request.system_text == _CHAT_PROMPT
            assert _authorization(request) == f"Bearer {_SECRET}"
            assert legacy_snapshot["output_instruction"] not in json.dumps(
                request.body,
                ensure_ascii=False,
            )

            for path in (
                f"{API_PREFIX}/providers",
                f"{API_PREFIX}/providers/fixture",
                f"{API_PREFIX}/providers/fixture/credential",
                f"{API_PREFIX}/prompts/chat",
            ):
                response = await http.get(path, headers=_AUTH)
                assert response.status_code == 200
                assert _SECRET not in response.text
                assert legacy_snapshot["output_instruction"] not in response.text
            audit_json = json.dumps(
                [event.to_dict() for event in audit_events],
                ensure_ascii=False,
            )
            assert _SECRET not in audit_json
            assert _CHAT_PROMPT not in audit_json
            assert server.base_url not in audit_json
    finally:
        logging.getLogger("komari_agent.host").removeHandler(capture)
        await host.close()
        server.close()

    assert _SECRET not in "\n".join(log_records)


async def test_restart_keeps_configuration_and_rejects_old_etags(
    app: App,
    tmp_path: Path,
) -> None:
    server = ScriptedModelServer()
    server.start()
    data_dir = tmp_path / "data"
    host = host_for(data_dir)
    await host.start()
    prompt_etag = ""
    try:
        api_app = _app(host)
        async with app.test_server(asgi=cast("Any", api_app)) as ctx:
            http = ctx.get_client()
            await _configure_model(
                http,
                server.base_url,
                chat_prompt=_CHAT_PROMPT,
                summary_prompt=_SUMMARY_PROMPT,
            )
            prompt = await http.get(f"{API_PREFIX}/prompts/chat", headers=_AUTH)
            assert prompt.json()["value"] == _CHAT_PROMPT
            provider = await http.get(
                f"{API_PREFIX}/providers/fixture",
                headers=_AUTH,
            )
            provider_edit = await http.put(
                f"{API_PREFIX}/providers/fixture",
                headers=_write_headers(provider.json()["etag"], "provider-edit-after-prompt"),
                json={
                    "api": "openai-completions",
                    "baseURL": server.base_url,
                    "displayName": "Renamed after prompt",
                    "models": [{"id": "fixture-model"}],
                },
            )
            assert provider_edit.status_code == 200
            binding = await http.get(f"{API_PREFIX}/bindings/chat", headers=_AUTH)
            binding_edit = await http.put(
                f"{API_PREFIX}/bindings/chat",
                headers=_write_headers(binding.json()["etag"], "binding-edit-after-prompt"),
                json={"provider": "fixture", "model": "fixture-model"},
            )
            assert binding_edit.status_code == 200
            prompt_after_edits = await http.get(
                f"{API_PREFIX}/prompts/chat",
                headers=_AUTH,
            )
            assert prompt_after_edits.json()["value"] == _CHAT_PROMPT
            prompt_etag = prompt_after_edits.json()["etag"]
    finally:
        await host.close()

    server.enqueue(ToolCallResponse("fixture_complete", {"value": "after-restart"}))
    restarted = host_for(data_dir)
    await restarted.start()
    try:
        api_app = _app(restarted)
        async with app.test_server(asgi=cast("Any", api_app)) as ctx:
            http = ctx.get_client()
            provider = await http.get(f"{API_PREFIX}/providers/fixture", headers=_AUTH)
            assert provider.status_code == 200
            assert provider.json()["provider"] == "fixture"
            binding = await http.get(f"{API_PREFIX}/bindings/chat", headers=_AUTH)
            assert binding.json()["model"] == "fixture-model"
            credential = await http.get(
                f"{API_PREFIX}/providers/fixture/credential",
                headers=_AUTH,
            )
            assert credential.json()["configured"] is True
            prompt = await http.get(f"{API_PREFIX}/prompts/chat", headers=_AUTH)
            assert prompt.json()["value"] == _CHAT_PROMPT
            assert prompt.json()["etag"] != prompt_etag
            stale = await http.put(
                f"{API_PREFIX}/prompts/chat",
                headers=_write_headers(prompt_etag, "stale-after-restart"),
                json={"value": "must not overwrite"},
            )
            assert stale.status_code == 409
            assert stale.json()["detail"]["code"] == "revision_conflict"
            unchanged = await http.get(f"{API_PREFIX}/prompts/chat", headers=_AUTH)
            assert unchanged.json()["value"] == _CHAT_PROMPT

            result = await restarted.execute(_task("restart-task"), _callbacks())
            assert result.result == {"ok": True, "value": "after-restart"}
    finally:
        await restarted.close()
        server.close()


async def test_live_task_blocks_connection_writes_and_freezes_selection(
    app: App,
    tmp_path: Path,
) -> None:
    server = ScriptedModelServer()
    server.start()
    server.enqueue(
        ToolCallResponse(
            "fixture_followup",
            {"text": "second turn", "conclude": True},
        ),
        ToolCallResponse("fixture_complete", {"value": "first-task"}),
        ToolCallResponse("fixture_complete", {"value": "second-task"}),
    )
    host = host_for(tmp_path / "data")
    await host.start()
    second_admission = asyncio.Event()
    release_second = asyncio.Event()

    async def admission(request: AgentCallbackRequest) -> JsonValue:
        attempt = request.data.get("attempt")
        if (
            request.data.get("purpose") == "conversation"
            and isinstance(attempt, int)
            and attempt >= 2
        ):
            second_admission.set()
            await release_second.wait()
        return {"allow": True}

    try:
        api_app = _app(host)
        async with app.test_server(asgi=cast("Any", api_app)) as ctx:
            http = ctx.get_client()
            await _configure_model(
                http,
                server.base_url,
                models=("model-a", "model-b"),
                chat_model="model-a",
                chat_prompt=_CHAT_PROMPT,
                summary_prompt=_SUMMARY_PROMPT,
                configure_summary=False,
            )

            task = asyncio.create_task(
                host.execute(_task("live-task"), _callbacks(admission=admission)),
            )
            await asyncio.wait_for(second_admission.wait(), timeout=20)

            provider_etag = (
                await http.get(f"{API_PREFIX}/providers", headers=_AUTH)
            ).json()["etag"]
            blocked_provider = await http.put(
                f"{API_PREFIX}/providers/fixture",
                headers=_write_headers(provider_etag, "blocked-provider"),
                json={
                    **_provider_body(server.base_url, models=("model-a", "model-b")),
                    "displayName": "must not save",
                },
            )
            assert blocked_provider.status_code == 409
            assert blocked_provider.json()["detail"]["code"] == "active_tasks"

            credential_etag = (
                await http.get(
                    f"{API_PREFIX}/providers/fixture/credential",
                    headers=_AUTH,
                )
            ).json()["etag"]
            blocked_credential = await http.put(
                f"{API_PREFIX}/providers/fixture/credential",
                headers=_write_headers(credential_etag, "blocked-credential"),
                json={"value": "must-not-save"},
            )
            assert blocked_credential.status_code == 409
            assert blocked_credential.json()["detail"]["code"] == "active_tasks"

            provider_after = await http.get(
                f"{API_PREFIX}/providers/fixture",
                headers=_AUTH,
            )
            assert provider_after.json()["displayName"] == "Controlled local provider"
            assert provider_after.json()["etag"] == provider_etag
            credential_after = await http.get(
                f"{API_PREFIX}/providers/fixture/credential",
                headers=_AUTH,
            )
            assert credential_after.json()["configured"] is True
            assert credential_after.json()["etag"] == credential_etag

            prompt_etag = (
                await http.get(f"{API_PREFIX}/prompts/chat", headers=_AUTH)
            ).json()["etag"]
            prompt_write = await http.put(
                f"{API_PREFIX}/prompts/chat",
                headers=_write_headers(prompt_etag, "live-prompt"),
                json={"value": "next task prompt"},
            )
            assert prompt_write.status_code == 200

            binding_etag = (
                await http.get(f"{API_PREFIX}/bindings/chat", headers=_AUTH)
            ).json()["etag"]
            binding_write = await http.put(
                f"{API_PREFIX}/bindings/chat",
                headers=_write_headers(binding_etag, "live-binding"),
                json={"provider": "fixture", "model": "model-b"},
            )
            assert binding_write.status_code == 200

            release_second.set()
            first = await asyncio.wait_for(task, timeout=20)
            assert first.result == {"ok": True, "value": "first-task"}
            assert len(server.requests) == 2
            assert all(
                request.system_text == _CHAT_PROMPT for request in server.requests[:2]
            )
            assert all(
                request.body.get("model") == "model-a"
                for request in server.requests[:2]
            )

            second = await host.execute(_task("next-task"), _callbacks())
            assert second.result == {"ok": True, "value": "second-task"}
            assert len(server.requests) == 3
            assert server.requests[2].system_text == "next task prompt"
            assert server.requests[2].body.get("model") == "model-b"
    finally:
        release_second.set()
        await host.close()
        server.close()


async def test_two_writes_with_the_same_etag_yield_one_success_and_one_conflict(
    app: App,
    tmp_path: Path,
) -> None:
    host = host_for(tmp_path / "data")
    await host.start()
    try:
        api_app = _app(host)
        async with app.test_server(asgi=cast("Any", api_app)) as ctx:
            http = ctx.get_client()
            await _configure_model(
                http,
                "http://127.0.0.1:9/v1",
                chat_prompt=_CHAT_PROMPT,
                summary_prompt=_SUMMARY_PROMPT,
                configure_summary=False,
            )
            current = await http.get(f"{API_PREFIX}/prompts/chat", headers=_AUTH)
            etag = current.json()["etag"]
            first, second = await asyncio.gather(
                http.put(
                    f"{API_PREFIX}/prompts/chat",
                    headers=_write_headers(etag, "concurrent-first"),
                    json={"value": "concurrent first value"},
                ),
                http.put(
                    f"{API_PREFIX}/prompts/chat",
                    headers=_write_headers(etag, "concurrent-second"),
                    json={"value": "concurrent second value"},
                ),
            )
            statuses = sorted([first.status_code, second.status_code])
            assert statuses == [200, 409]
            conflict = first if first.status_code == 409 else second
            assert conflict.json()["detail"]["code"] == "revision_conflict"
            final = await http.get(f"{API_PREFIX}/prompts/chat", headers=_AUTH)
            assert final.json()["value"] in {
                "concurrent first value",
                "concurrent second value",
            }
    finally:
        await host.close()


async def test_child_exit_is_reported_as_a_safe_503(
    app: App,
    tmp_path: Path,
) -> None:
    host = host_for(tmp_path / "data")
    await host.start()
    try:
        api_app = _app(host)
        async with app.test_server(asgi=cast("Any", api_app)) as ctx:
            http = ctx.get_client()
            await _configure_model(
                http,
                "http://127.0.0.1:9/v1",
                chat_prompt=_CHAT_PROMPT,
                summary_prompt=_SUMMARY_PROMPT,
            )
            pid = host.process_id
            assert pid is not None
            os.kill(pid, signal.SIGKILL)
            for _ in range(100):
                if host.readiness.state is ReadinessState.UNAVAILABLE:
                    break
                await asyncio.sleep(0.05)
            assert host.readiness.state is ReadinessState.UNAVAILABLE
            failed = await http.get(f"{API_PREFIX}/providers", headers=_AUTH)
            assert failed.status_code == 503
            assert failed.json()["detail"]["code"] == "unavailable"
            assert _SECRET not in failed.text
    finally:
        await host.close()


@pytest.mark.parametrize("case", ["duplicate_models", "off_only", "empty_off"])
async def test_invalid_provider_semantics_are_422_without_write(
    app: App, tmp_path: Path, case: str,
) -> None:
    host = host_for(tmp_path / "data")
    await host.start()
    try:
        async with app.test_server(asgi=cast("Any", _app(host))) as ctx:
            http = ctx.get_client()
            before = await http.get(f"{API_PREFIX}/providers", headers=_AUTH)
            body = _provider_body("http://127.0.0.1:9/v1")
            match case:
                case "duplicate_models":
                    body["models"].append(dict(body["models"][0]))
                case "off_only":
                    body["models"][0]["reasoningEfforts"] = {"off": None}
                case "empty_off":
                    body["models"][0]["reasoningEfforts"] = {"off": "", "low": "low"}
            response = await http.put(
                f"{API_PREFIX}/providers/fixture",
                headers=_write_headers(before.json()["etag"], f"invalid-{case}"),
                json=body,
            )
            assert response.status_code == 422
            assert response.json()["detail"]["code"] == "invalid_provider"
            after = await http.get(f"{API_PREFIX}/providers", headers=_AUTH)
            assert after.json() == before.json()
            valid = await http.put(
                f"{API_PREFIX}/providers/fixture",
                headers=_write_headers(before.json()["etag"], "valid-after-invalid"),
                json=_provider_body("http://127.0.0.1:9/v1"),
            )
            assert valid.status_code == 200
    finally:
        await host.close()


async def test_inherited_object_name_is_not_a_configured_provider(
    app: App, tmp_path: Path,
) -> None:
    host = host_for(tmp_path / "data")
    await host.start()
    try:
        async with app.test_server(asgi=cast("Any", _app(host))) as ctx:
            http = ctx.get_client()
            before = await http.get(f"{API_PREFIX}/providers", headers=_AUTH)
            missing = await http.get(f"{API_PREFIX}/providers/constructor", headers=_AUTH)
            assert missing.status_code == 404
            assert missing.json()["detail"]["code"] == "not_found"
            deletion = await http.delete(
                f"{API_PREFIX}/providers/constructor",
                headers=_write_headers(before.json()["etag"], "missing-constructor"),
            )
            assert deletion.status_code == 404
            assert deletion.json()["detail"]["code"] == "not_found"
            after = await http.get(f"{API_PREFIX}/providers", headers=_AUTH)
            assert after.json() == before.json()
    finally:
        await host.close()
