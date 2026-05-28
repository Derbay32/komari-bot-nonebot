"""Komari Management 配置接口路由测试。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from komari_bot.plugins.komari_management.config_api import (
    API_PREFIX,
    register_config_api,
)
from komari_bot.plugins.komari_management.managed_resources import (
    ManagedConfigResource,
)

if TYPE_CHECKING:
    from nonebug import App


class _ConfigSchema(BaseModel):
    plugin_enable: bool = True
    api_token: str = "secret"
    last_updated: str = "2026-04-14T00:00:00+08:00"


class _FakeConfigManager:
    def __init__(self) -> None:
        self.config = _ConfigSchema()
        self.config_file = Path("/tmp/komari_management_test.json")
        self.reload_count = 0

    def get(self) -> _ConfigSchema:
        return self.config

    def update_field(self, field_name: str, value: object) -> _ConfigSchema:
        data = self.config.model_dump()
        if field_name not in data:
            detail = f"未知的配置字段: {field_name}"
            raise ValueError(detail)
        data[field_name] = value
        self.config = _ConfigSchema(**data)
        return self.config

    def reload_from_json(self) -> _ConfigSchema:
        self.reload_count += 1
        return self.config


def _build_app(manager: _FakeConfigManager) -> FastAPI:
    api_app = FastAPI()
    register_config_api(
        api_app,
        api_token="secret-token",
        allowed_origins=["https://ui.example.com"],
        resources=(
            ManagedConfigResource(
                resource_id="komari_management",
                display_name="Komari Management",
                manager_getter=lambda: manager,
            ),
        ),
    )
    return api_app


@pytest.mark.asyncio
async def test_config_routes_require_token_and_list_resources(app: App) -> None:
    manager = _FakeConfigManager()
    async with app.test_server(asgi=cast("Any", _build_app(manager))) as ctx:
        client = ctx.get_client()
        unauthorized = await client.get(f"{API_PREFIX}/resources")
        listed = await client.get(
            f"{API_PREFIX}/resources",
            headers={"Authorization": "Bearer secret-token"},
        )

    assert unauthorized.status_code == 401
    assert listed.status_code == 200
    payload = listed.json()
    assert payload["total"] == 1
    assert payload["items"][0]["resource_id"] == "komari_management"


@pytest.mark.asyncio
async def test_config_routes_support_detail_reload_and_field_update(app: App) -> None:
    manager = _FakeConfigManager()
    headers = {"Authorization": "Bearer secret-token"}

    async with app.test_server(asgi=cast("Any", _build_app(manager))) as ctx:
        client = ctx.get_client()
        detail = await client.get(
            f"{API_PREFIX}/resources/komari_management", headers=headers
        )
        updated = await client.patch(
            f"{API_PREFIX}/resources/komari_management/fields/api_token",
            json={"value": "changed-token"},
            headers=headers,
        )
        reloaded = await client.post(
            f"{API_PREFIX}/resources/komari_management/reload",
            headers=headers,
        )

    assert detail.status_code == 200
    assert detail.json()["values"]["api_token"] == "secret"
    assert updated.status_code == 200
    assert updated.json()["values"]["api_token"] == "changed-token"
    assert reloaded.status_code == 200
    assert manager.reload_count == 1


@pytest.mark.asyncio
async def test_config_routes_report_validation_and_not_found(app: App) -> None:
    manager = _FakeConfigManager()
    headers = {"Authorization": "Bearer secret-token"}

    async with app.test_server(asgi=cast("Any", _build_app(manager))) as ctx:
        client = ctx.get_client()
        missing_resource = await client.get(
            f"{API_PREFIX}/resources/missing",
            headers=headers,
        )
        missing_field = await client.patch(
            f"{API_PREFIX}/resources/komari_management/fields/missing_field",
            json={"value": "anything"},
            headers=headers,
        )

    assert missing_resource.status_code == 404
    assert missing_field.status_code == 422
