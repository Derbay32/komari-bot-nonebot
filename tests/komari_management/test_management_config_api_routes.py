"""Komari Management 配置接口路由测试。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field

from komari_bot.plugins.config_manager.manager import ConfigUpdateConflictError
from komari_bot.plugins.komari_management.config_api import (
    API_PREFIX,
    register_config_api,
)
from komari_bot.plugins.komari_management.managed_resources import (
    ManagedConfigResource,
)

if TYPE_CHECKING:
    from nonebug import App

    from komari_bot.common.management_audit import ManagementAuditEvent


class _ConfigSchema(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"default_apply_mode": "immediate"},
    )

    plugin_enable: bool = Field(default=True, description="插件启用状态")
    api_token: str = Field(
        default="secret",
        description="管理接口令牌",
        json_schema_extra={"secret": True},
    )
    embedding_api_key: str = Field(
        default="embedding-secret",
        description="嵌入 API 密钥",
        json_schema_extra={"secret": True},
    )
    db_password: str = Field(
        default="database-secret",
        description="数据库密码",
        json_schema_extra={"secret": True},
    )
    dsn: str = Field(
        default="https://sentry-canary@example.invalid/1",
        description="Sentry DSN",
        json_schema_extra={"secret": True},
    )
    monkey_mode: str = Field(
        default="visible",
        description="名称含 key 但不是秘密的普通字段",
    )
    api_allowed_origins: list[str] = Field(
        default_factory=lambda: ["https://old.example.com"],
        description="管理接口 CORS Origin",
        json_schema_extra={"apply_mode": "restart"},
    )
    last_updated: str = Field(
        default="2026-04-14T00:00:00+08:00",
        description="最后更新时间",
    )


class _FakeConfigManager:
    def __init__(self) -> None:
        self.config = _ConfigSchema()
        self.config_source = "postgres:komari_plugin_configs/komari_management"
        self.reload_count = 0

    async def get_async(self) -> _ConfigSchema:
        return self.config

    async def update_field_async(
        self, field_name: str, value: object
    ) -> _ConfigSchema:
        data = self.config.model_dump()
        if field_name not in data:
            detail = f"未知的配置字段: {field_name}"
            raise ValueError(detail)
        data[field_name] = value
        self.config = _ConfigSchema(**data)
        return self.config

    async def reload_async(self) -> _ConfigSchema:
        self.reload_count += 1
        return self.config


class _ConflictConfigManager(_FakeConfigManager):
    async def update_field_async(
        self, field_name: str, value: object
    ) -> _ConfigSchema:
        del field_name, value
        msg = "配置已被其他进程连续修改，请重试"
        raise ConfigUpdateConflictError(msg)


def _write_headers(request_id: str) -> dict[str, str]:
    return {
        "Authorization": "Bearer secret-token-00000000",
        "X-Komari-Change-Reason": "验证配置变更",
        "X-Request-ID": request_id,
    }


def _build_app(
    manager: _FakeConfigManager,
    audit_events: list[ManagementAuditEvent] | None = None,
) -> FastAPI:
    async def _record_audit(event: ManagementAuditEvent) -> None:
        if audit_events is not None:
            audit_events.append(event)

    api_app = FastAPI()
    register_config_api(
        api_app,
        api_token="secret-token-00000000",
        allowed_origins=["https://ui.example.com"],
        resources=(
            ManagedConfigResource(
                resource_id="komari_management",
                display_name="Komari Management",
                manager_getter=lambda: manager,
            ),
        ),
        audit_recorder=_record_audit,
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
            headers={"Authorization": "Bearer secret-token-00000000"},
        )

    assert unauthorized.status_code == 401
    assert listed.status_code == 200
    payload = listed.json()
    assert payload["total"] == 1
    assert payload["items"][0]["resource_id"] == "komari_management"
    assert payload["items"][0]["config_source"] == manager.config_source
    assert payload["items"][0]["field_descriptions"] == {
        "api_allowed_origins": "管理接口 CORS Origin",
        "api_token": "管理接口令牌",
        "db_password": "数据库密码",
        "dsn": "Sentry DSN",
        "embedding_api_key": "嵌入 API 密钥",
        "last_updated": "最后更新时间",
        "monkey_mode": "名称含 key 但不是秘密的普通字段",
        "plugin_enable": "插件启用状态",
    }
    assert payload["items"][0]["field_metadata"]["dsn"] == {
        "secret": True,
        "apply_mode": "immediate",
    }
    assert payload["items"][0]["field_metadata"]["api_allowed_origins"] == {
        "secret": False,
        "apply_mode": "restart",
    }


@pytest.mark.asyncio
async def test_config_routes_support_detail_reload_and_field_update(app: App) -> None:
    manager = _FakeConfigManager()
    audit_events: list[ManagementAuditEvent] = []
    read_headers = {"Authorization": "Bearer secret-token-00000000"}

    async with app.test_server(
        asgi=cast("Any", _build_app(manager, audit_events))
    ) as ctx:
        client = ctx.get_client()
        detail = await client.get(
            f"{API_PREFIX}/resources/komari_management", headers=read_headers
        )
        updated = await client.patch(
            f"{API_PREFIX}/resources/komari_management/fields/api_token",
            json={"value": "changed-token"},
            headers=_write_headers("config-token-update"),
        )
        restart_updated = await client.patch(
            f"{API_PREFIX}/resources/komari_management/fields/api_allowed_origins",
            json={"value": ["https://new.example.com"]},
            headers=_write_headers("config-origin-update"),
        )
        reloaded = await client.post(
            f"{API_PREFIX}/resources/komari_management/reload",
            headers=_write_headers("config-reload"),
        )

    assert detail.status_code == 200
    assert detail.json()["values"]["api_token"] == "******"
    assert detail.json()["values"]["embedding_api_key"] == "******"
    assert detail.json()["values"]["db_password"] == "******"
    assert detail.json()["values"]["dsn"] == "******"
    assert detail.json()["values"]["monkey_mode"] == "visible"
    assert detail.json()["values"]["plugin_enable"] is True
    assert detail.json()["values"]["last_updated"] == "2026-04-14T00:00:00+08:00"
    assert detail.json()["config_source"] == manager.config_source
    assert detail.json()["field_descriptions"]["api_token"] == "管理接口令牌"
    assert updated.status_code == 200
    assert updated.json()["values"]["api_token"] == "******"
    assert updated.json()["field_descriptions"]["api_token"] == "管理接口令牌"
    assert updated.json()["field_states"]["api_token"] == {
        "secret": True,
        "apply_mode": "immediate",
        "configured_value": "******",
        "effective_value": "******",
        "source": manager.config_source,
        "effective_source": "dynamic_config",
        "restart_required": False,
    }
    assert restart_updated.status_code == 200
    assert restart_updated.json()["field_states"]["api_allowed_origins"] == {
        "secret": False,
        "apply_mode": "restart",
        "configured_value": ["https://new.example.com"],
        "effective_value": None,
        "source": manager.config_source,
        "effective_source": "process_startup_snapshot",
        "restart_required": True,
    }
    assert reloaded.status_code == 200
    assert reloaded.json()["values"]["api_token"] == "******"
    assert reloaded.json()["field_descriptions"]["plugin_enable"] == "插件启用状态"
    assert manager.reload_count == 1
    assert [event.outcome for event in audit_events] == [
        "started",
        "succeeded",
        "started",
        "succeeded",
        "started",
        "succeeded",
    ]
    token_events = [
        event for event in audit_events if event.request_id == "config-token-update"
    ]
    assert {event.operator_id for event in token_events} == {"legacy-api-token"}
    assert {event.resource for event in token_events} == {"komari_management"}
    assert {event.field_name for event in token_events} == {"api_token"}
    serialized_events = json.dumps(
        [event.to_dict() for event in audit_events],
        ensure_ascii=False,
    )
    assert "changed-token" not in serialized_events


@pytest.mark.asyncio
async def test_config_routes_report_validation_and_not_found(app: App) -> None:
    manager = _FakeConfigManager()
    read_headers = {"Authorization": "Bearer secret-token-00000000"}

    async with app.test_server(asgi=cast("Any", _build_app(manager))) as ctx:
        client = ctx.get_client()
        missing_resource = await client.get(
            f"{API_PREFIX}/resources/missing",
            headers=read_headers,
        )
        missing_field = await client.patch(
            f"{API_PREFIX}/resources/komari_management/fields/missing_field",
            json={"value": "anything"},
            headers=_write_headers("config-missing-field"),
        )

    assert missing_resource.status_code == 404
    assert missing_field.status_code == 422


@pytest.mark.asyncio
async def test_config_route_reports_revision_conflict_as_409(app: App) -> None:
    manager = _ConflictConfigManager()
    audit_events: list[ManagementAuditEvent] = []

    async with app.test_server(
        asgi=cast("Any", _build_app(manager, audit_events))
    ) as ctx:
        response = await ctx.get_client().patch(
            f"{API_PREFIX}/resources/komari_management/fields/plugin_enable",
            json={"value": False},
            headers=_write_headers("config-conflict"),
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "配置已被其他进程连续修改，请重试"
    assert [event.outcome for event in audit_events] == ["started", "failed"]
    assert audit_events[-1].status_code == 409
    assert audit_events[-1].request_id == "config-conflict"


@pytest.mark.asyncio
async def test_config_write_requires_change_reason(app: App) -> None:
    manager = _FakeConfigManager()

    async with app.test_server(asgi=cast("Any", _build_app(manager))) as ctx:
        response = await ctx.get_client().patch(
            f"{API_PREFIX}/resources/komari_management/fields/plugin_enable",
            json={"value": False},
            headers={"Authorization": "Bearer secret-token-00000000"},
        )

    assert response.status_code == 400
    assert manager.config.plugin_enable is True
