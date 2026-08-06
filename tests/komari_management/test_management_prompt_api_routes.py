"""Komari Management Prompt 接口路由测试。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi import FastAPI

from komari_bot.common.prompt_storage import PromptValues, StoredPrompt
from komari_bot.plugins.komari_management.managed_resources import ManagedPromptResource
from komari_bot.plugins.komari_management.prompt_api import (
    API_PREFIX,
    register_prompt_api,
)

if TYPE_CHECKING:
    from nonebug import App

    from komari_bot.common.management_audit import ManagementAuditEvent


@dataclass
class _PromptStore:
    values: dict[str, str]
    revision: int = 1


def _write_headers(request_id: str, revision: int) -> dict[str, str]:
    return {
        "Authorization": "Bearer secret-token-00000000",
        "X-Komari-Change-Reason": "验证提示词变更",
        "X-Request-ID": request_id,
        "If-Match": f'"{revision}"',
    }


def _build_app(
    monkeypatch: pytest.MonkeyPatch,
    store: _PromptStore,
    audit_events: list[ManagementAuditEvent] | None = None,
) -> FastAPI:
    from komari_bot.plugins.komari_management import prompt_api

    async def fake_load_prompt_values(resource: ManagedPromptResource) -> PromptValues:
        values = dict(resource.defaults)
        values.update(store.values)
        stored = StoredPrompt(
            resource_id=resource.resource_id,
            prompt_data=dict(store.values),
            revision=store.revision,
            updated_at=datetime.now(UTC),
        )
        return PromptValues(values=values, stored=stored)

    async def fake_replace_prompt_values(
        resource: ManagedPromptResource,
        values: dict[str, object],
        *,
        expected_revision: int,
    ) -> StoredPrompt | None:
        if expected_revision != store.revision:
            return None
        unknown_fields = sorted(set(values) - set(resource.defaults))
        if unknown_fields:
            fields = ", ".join(unknown_fields)
            msg = f"存在未知提示词字段: {fields}"
            raise ValueError(msg)
        cleaned = dict(resource.defaults)
        for key, value in values.items():
            if not isinstance(value, str) or not value.strip():
                msg = f"提示词字段 {key} 必须是非空字符串"
                raise ValueError(msg)
            cleaned[key] = value.rstrip("\n")
        store.values = cleaned
        store.revision += 1
        return StoredPrompt(
            resource_id=resource.resource_id,
            prompt_data=dict(cleaned),
            revision=store.revision,
            updated_at=datetime.now(UTC),
        )

    async def fake_update_prompt_field(
        resource: ManagedPromptResource,
        field_name: str,
        value: object,
        *,
        expected_revision: int,
    ) -> StoredPrompt | None:
        if expected_revision != store.revision:
            return None
        if field_name not in resource.defaults:
            msg = f"存在未知提示词字段: {field_name}"
            raise ValueError(msg)
        if not isinstance(value, str) or not value.strip():
            msg = f"提示词字段 {field_name} 必须是非空字符串"
            raise ValueError(msg)
        store.values[field_name] = value.rstrip("\n")
        store.revision += 1
        return StoredPrompt(
            resource_id=resource.resource_id,
            prompt_data=dict(store.values),
            revision=store.revision,
            updated_at=datetime.now(UTC),
        )

    monkeypatch.setattr(
        prompt_api,
        "load_prompt_values_async",
        fake_load_prompt_values,
    )
    monkeypatch.setattr(
        prompt_api,
        "replace_prompt_values_async",
        fake_replace_prompt_values,
    )
    monkeypatch.setattr(
        prompt_api,
        "update_prompt_field_async",
        fake_update_prompt_field,
    )

    async def _record_audit(event: ManagementAuditEvent) -> None:
        if audit_events is not None:
            audit_events.append(event)

    api_app = FastAPI()
    register_prompt_api(
        api_app,
        api_token=[
            {
                "credential_id": "prompt-api-operator",
                "token": "secret-token-00000000",
                "permissions": ["*"],
            }
        ],
        allowed_origins=["https://ui.example.com"],
        resources=(
            ManagedPromptResource(
                resource_id="komari_chat",
                display_name="Komari Chat Prompt",
                defaults={
                    "system_prompt": "默认系统提示词",
                    "memory_ack": "默认确认",
                },
            ),
        ),
        audit_recorder=_record_audit,
    )
    return api_app


@pytest.mark.asyncio
async def test_prompt_routes_require_token_and_list_resources(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _PromptStore(values={"system_prompt": "你好", "memory_ack": "收到"})

    async with app.test_server(asgi=cast("Any", _build_app(monkeypatch, store))) as ctx:
        client = ctx.get_client()
        unauthorized = await client.get(f"{API_PREFIX}/resources")
        listed = await client.get(
            f"{API_PREFIX}/resources",
            headers={"Authorization": "Bearer secret-token-00000000"},
        )

    assert unauthorized.status_code == 401
    assert listed.status_code == 200
    assert listed.json()["items"][0]["resource_id"] == "komari_chat"
    assert listed.json()["items"][0]["file_path"] is None
    assert listed.json()["items"][0]["storage_key"] == "komari_chat"
    assert (
        listed.json()["items"][0]["config_source"]
        == "postgresql:komari_prompt_komari_chat:komari_chat"
    )


@pytest.mark.asyncio
async def test_prompt_routes_support_detail_replace_and_field_update(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _PromptStore(values={"system_prompt": "你好", "memory_ack": "收到"})
    read_headers = {"Authorization": "Bearer secret-token-00000000"}

    async with app.test_server(asgi=cast("Any", _build_app(monkeypatch, store))) as ctx:
        client = ctx.get_client()
        detail = await client.get(
            f"{API_PREFIX}/resources/komari_chat", headers=read_headers
        )
        updated = await client.patch(
            f"{API_PREFIX}/resources/komari_chat/fields/system_prompt",
            json={"value": "新的系统提示词"},
            headers=_write_headers("prompt-field-update", 1),
        )
        replaced = await client.put(
            f"{API_PREFIX}/resources/komari_chat",
            json={"system_prompt": "完整替换", "memory_ack": "也替换"},
            headers=_write_headers("prompt-replace", 2),
        )

    assert detail.status_code == 200
    assert detail.json()["values"]["system_prompt"] == "你好"
    assert detail.json()["revision"] == 1
    assert (
        detail.json()["config_source"]
        == "postgresql:komari_prompt_komari_chat:komari_chat"
    )
    assert updated.status_code == 200
    assert updated.json()["values"]["system_prompt"] == "新的系统提示词"
    assert replaced.status_code == 200
    assert replaced.json()["values"]["memory_ack"] == "也替换"


@pytest.mark.asyncio
async def test_prompt_routes_report_validation_and_not_found(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _PromptStore(values={"system_prompt": "你好", "memory_ack": "收到"})
    read_headers = {"Authorization": "Bearer secret-token-00000000"}

    async with app.test_server(asgi=cast("Any", _build_app(monkeypatch, store))) as ctx:
        client = ctx.get_client()
        missing_resource = await client.get(
            f"{API_PREFIX}/resources/missing",
            headers=read_headers,
        )
        missing_field = await client.patch(
            f"{API_PREFIX}/resources/komari_chat/fields/missing_field",
            json={"value": "anything"},
            headers=_write_headers("prompt-missing-field", 1),
        )
        invalid_replace = await client.put(
            f"{API_PREFIX}/resources/komari_chat",
            json={"unknown": "anything"},
            headers=_write_headers("prompt-invalid-replace", 1),
        )

    assert missing_resource.status_code == 404
    assert missing_field.status_code == 404
    assert invalid_replace.status_code == 422


@pytest.mark.asyncio
async def test_prompt_writes_require_matching_revision(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _PromptStore(values={"system_prompt": "你好", "memory_ack": "收到"})

    async with app.test_server(asgi=cast("Any", _build_app(monkeypatch, store))) as ctx:
        client = ctx.get_client()
        missing_header = await client.patch(
            f"{API_PREFIX}/resources/komari_chat/fields/system_prompt",
            json={"value": "不会写入"},
            headers={
                "Authorization": "Bearer secret-token-00000000",
                "X-Komari-Change-Reason": "验证缺少修订号",
            },
        )
        stale_revision = await client.patch(
            f"{API_PREFIX}/resources/komari_chat/fields/system_prompt",
            json={"value": "也不会写入"},
            headers=_write_headers("prompt-stale-revision", 0),
        )

    assert missing_header.status_code == 422
    assert stale_revision.status_code == 409
    assert store.values["system_prompt"] == "你好"
    assert store.revision == 1
