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


@dataclass
class _PromptStore:
    values: dict[str, str]


def _build_app(monkeypatch: pytest.MonkeyPatch, store: _PromptStore) -> FastAPI:
    from komari_bot.plugins.komari_management import prompt_api

    def fake_load_prompt_values(resource: ManagedPromptResource) -> PromptValues:
        values = dict(resource.defaults)
        values.update(store.values)
        stored = StoredPrompt(
            resource_id=resource.resource_id,
            display_name=resource.display_name,
            prompt_data=dict(store.values),
            version="1.0",
            updated_at=datetime.now(UTC),
        )
        return PromptValues(values=values, stored=stored)

    def fake_save_prompt_values(
        resource: ManagedPromptResource,
        values: dict[str, object],
    ) -> StoredPrompt:
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
        return StoredPrompt(
            resource_id=resource.resource_id,
            display_name=resource.display_name,
            prompt_data=dict(cleaned),
            version="1.0",
            updated_at=datetime.now(UTC),
        )

    monkeypatch.setattr(prompt_api, "load_prompt_values", fake_load_prompt_values)
    monkeypatch.setattr(prompt_api, "save_prompt_values", fake_save_prompt_values)

    api_app = FastAPI()
    register_prompt_api(
        api_app,
        api_token="secret-token",
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
            headers={"Authorization": "Bearer secret-token"},
        )

    assert unauthorized.status_code == 401
    assert listed.status_code == 200
    assert listed.json()["items"][0]["resource_id"] == "komari_chat"
    assert listed.json()["items"][0]["file_path"] is None
    assert listed.json()["items"][0]["storage_key"] == "komari_chat"


@pytest.mark.asyncio
async def test_prompt_routes_support_detail_replace_and_field_update(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _PromptStore(values={"system_prompt": "你好", "memory_ack": "收到"})
    headers = {"Authorization": "Bearer secret-token"}

    async with app.test_server(asgi=cast("Any", _build_app(monkeypatch, store))) as ctx:
        client = ctx.get_client()
        detail = await client.get(
            f"{API_PREFIX}/resources/komari_chat", headers=headers
        )
        updated = await client.patch(
            f"{API_PREFIX}/resources/komari_chat/fields/system_prompt",
            json={"value": "新的系统提示词"},
            headers=headers,
        )
        replaced = await client.put(
            f"{API_PREFIX}/resources/komari_chat",
            json={"system_prompt": "完整替换", "memory_ack": "也替换"},
            headers=headers,
        )

    assert detail.status_code == 200
    assert detail.json()["values"]["system_prompt"] == "你好"
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
    headers = {"Authorization": "Bearer secret-token"}

    async with app.test_server(asgi=cast("Any", _build_app(monkeypatch, store))) as ctx:
        client = ctx.get_client()
        missing_resource = await client.get(
            f"{API_PREFIX}/resources/missing",
            headers=headers,
        )
        missing_field = await client.patch(
            f"{API_PREFIX}/resources/komari_chat/fields/missing_field",
            json={"value": "anything"},
            headers=headers,
        )
        invalid_replace = await client.put(
            f"{API_PREFIX}/resources/komari_chat",
            json={"unknown": "anything"},
            headers=headers,
        )

    assert missing_resource.status_code == 404
    assert missing_field.status_code == 404
    assert invalid_replace.status_code == 422
