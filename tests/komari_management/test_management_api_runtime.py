"""Komari Management API 挂载测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from komari_bot.plugins.agent_run_logger.api import register_agent_run_log_api
from komari_bot.plugins.komari_help.api import register_help_api
from komari_bot.plugins.komari_knowledge.api import register_knowledge_api
from komari_bot.plugins.komari_management.api_runtime import (
    ManagementApiComponents,
    register_management_api_for_driver,
)
from komari_bot.plugins.komari_management.managed_resources import (
    ManagedConfigResource,
    ManagedPromptResource,
)
from komari_bot.plugins.komari_memory.api import register_memory_api
from komari_bot.plugins.user_ban.api import register_user_ban_api

if TYPE_CHECKING:
    from nonebug import App


class _FakeDriver:
    def __init__(self, driver_type: str, server_app: FastAPI | None = None) -> None:
        self.type = driver_type
        self.server_app = server_app


class _FakeLogger:
    def __init__(self) -> None:
        self.info_messages: list[str] = []
        self.warning_messages: list[str] = []

    @staticmethod
    def _render(message: str, args: tuple[object, ...]) -> str:
        if not args:
            return message
        try:
            return message.format(*args)
        except Exception:
            try:
                return message % args
            except Exception:
                return " ".join([message, *(str(arg) for arg in args)])

    def info(self, message: str, *args: object) -> None:
        self.info_messages.append(self._render(message, args))

    def warning(self, message: str, *args: object) -> None:
        self.warning_messages.append(self._render(message, args))


class _FakeConfigSchema(BaseModel):
    plugin_enable: bool = True


class _FakeConfigManager:
    @property
    def config_source(self) -> str:
        return "postgres:komari_plugin_configs/komari_management"

    async def get_async(self) -> BaseModel:
        return _FakeConfigSchema()

    async def update_field_async(
        self, field_name: str, value: object
    ) -> BaseModel:
        del field_name, value
        return await self.get_async()

    async def reload_async(self) -> BaseModel:
        return await self.get_async()


def _build_api_app() -> FastAPI:
    return FastAPI(
        docs_url="/api/komari-management/docs",
        openapi_url="/api/komari-management/openapi.json",
        redoc_url=None,
    )


def _build_components() -> ManagementApiComponents:
    return ManagementApiComponents(
        register_knowledge_api=register_knowledge_api,
        knowledge_engine_getter=lambda: None,
        register_help_api=register_help_api,
        help_engine_getter=lambda: None,
        register_memory_api=register_memory_api,
        memory_service_getter=lambda: None,
        memory_redis_getter=lambda: None,
        register_agent_run_log_api=register_agent_run_log_api,
        agent_run_log_reader_getter=lambda: None,
        register_user_ban_api=register_user_ban_api,
        user_ban_service_getter=lambda: None,
        config_resources=(
            ManagedConfigResource(
                resource_id="komari_management",
                display_name="Komari Management",
                manager_getter=lambda: _FakeConfigManager(),
            ),
        ),
        prompt_resources=(
            ManagedPromptResource(
                resource_id="komari_chat",
                display_name="Komari Chat Prompt",
                defaults={"system_prompt": "默认值"},
                legacy_file_path=Path("config") / "prompts" / "komari_memory.yaml",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_register_management_api_for_fastapi_driver(app: App) -> None:
    api_app = _build_api_app()
    logger = _FakeLogger()
    config = SimpleNamespace(
        plugin_enable=True,
        api_token="secret-token-00000000",
        api_allowed_origins=["https://ui.example.com"],
    )

    registered = register_management_api_for_driver(
        driver=_FakeDriver("fastapi", api_app),
        config=config,
        component_loader=_build_components,
        logger=logger,
    )

    assert registered is True

    async with app.test_server(asgi=cast("Any", api_app)) as ctx:
        client = ctx.get_client()
        docs = await client.get("/api/komari-management/docs")
        schema_response = await client.get("/api/komari-management/openapi.json")

    assert docs.status_code == 200
    assert schema_response.status_code == 200
    schema = schema_response.json()
    assert "/api/komari-knowledge/v1/knowledge" in schema["paths"]
    assert "/api/komari-help/v1/help" in schema["paths"]
    assert "/api/komari-memory/v1/conversations" in schema["paths"]
    assert "/api/llm-provider/v1/reply-logs" in schema["paths"]
    assert "/api/agent-run-logs/v1/runs" in schema["paths"]
    assert "/api/komari-management-config/v1/resources" in schema["paths"]
    assert "/api/komari-management-prompt/v1/resources" in schema["paths"]
    assert "/api/komari-announce/v1/groups" in schema["paths"]
    assert "/api/komari-announce/v1/maintenance" in schema["paths"]
    assert "/api/komari-decision-scenes/v1/scenes" in schema["paths"]
    assert "/api/komari-user-bans/v1/bans" in schema["paths"]
    security_schemes = schema["components"]["securitySchemes"]
    assert any(
        item.get("type") == "http" and item.get("scheme") == "bearer"
        for item in security_schemes.values()
    )
    assert logger.info_messages[-2] == (
        "[Komari Management] 管理 API 已注册: "
        "/api/komari-knowledge/v1, /api/komari-help/v1, /api/komari-memory/v1, "
        "/api/agent-run-logs/v1, /api/llm-provider/v1, "
        "/api/komari-management-config/v1, /api/komari-management-prompt/v1, /api/komari-announce/v1, "
        "/api/komari-decision-scenes/v1, /api/komari-user-bans/v1"
    )
    assert logger.info_messages[-1] == (
        "[Komari Management] 管理文档入口: "
        "docs=/api/komari-management/docs, "
        "openapi=/api/komari-management/openapi.json"
    )
    assert len(logger.warning_messages) == 1
    assert "旧版 api_token 已弃用" in logger.warning_messages[0]


@pytest.mark.asyncio
async def test_registered_api_uses_current_token_after_rotation(app: App) -> None:
    api_app = _build_api_app()
    logger = _FakeLogger()
    token_state = {"value": "old-token-00000000"}
    config = SimpleNamespace(
        plugin_enable=True,
        api_token="old-token-00000000",
        api_allowed_origins=[],
    )

    registered = register_management_api_for_driver(
        driver=_FakeDriver("fastapi", api_app),
        config=config,
        component_loader=_build_components,
        logger=logger,
        api_token_getter=lambda: token_state["value"],
    )

    assert registered is True
    async with app.test_server(asgi=cast("Any", api_app)) as ctx:
        client = ctx.get_client()
        before_rotation = await client.get(
            "/api/komari-knowledge/v1/knowledge",
            headers={"Authorization": "Bearer old-token-00000000"},
        )

        token_state["value"] = "new-token-00000000"
        old_token_after_rotation = await client.get(
            "/api/komari-knowledge/v1/knowledge",
            headers={"Authorization": "Bearer old-token-00000000"},
        )
        new_token_after_rotation = await client.get(
            "/api/komari-knowledge/v1/knowledge",
            headers={"Authorization": "Bearer new-token-00000000"},
        )

    assert before_rotation.status_code == 503
    assert old_token_after_rotation.status_code == 401
    assert new_token_after_rotation.status_code == 503


@pytest.mark.asyncio
async def test_registered_api_prefers_named_credentials_over_legacy_token(
    app: App,
) -> None:
    api_app = _build_api_app()
    logger = _FakeLogger()
    config = SimpleNamespace(
        plugin_enable=True,
        api_token="legacy-token-00000000",
        api_credentials=[
            {
                "credential_id": "knowledge-reader",
                "token": "knowledge-token-00000",
                "permissions": ["knowledge:read"],
            }
        ],
        api_allowed_origins=[],
    )

    registered = register_management_api_for_driver(
        driver=_FakeDriver("fastapi", api_app),
        config=config,
        component_loader=_build_components,
        logger=logger,
    )

    assert registered is True
    async with app.test_server(asgi=cast("Any", api_app)) as ctx:
        client = ctx.get_client()
        legacy = await client.get(
            "/api/komari-knowledge/v1/knowledge",
            headers={"Authorization": "Bearer legacy-token-00000000"},
        )
        named = await client.get(
            "/api/komari-knowledge/v1/knowledge",
            headers={"Authorization": "Bearer knowledge-token-00000"},
        )

    assert legacy.status_code == 401
    assert named.status_code == 503


@pytest.mark.asyncio
async def test_read_only_credential_cannot_mutate_any_management_resource(
    app: App,
) -> None:
    api_app = _build_api_app()
    logger = _FakeLogger()
    config = SimpleNamespace(
        plugin_enable=True,
        api_token="",
        api_credentials=[
            {
                "credential_id": "dashboard-reader",
                "token": "read-only-token-000000",
                "permissions": [
                    "knowledge:read",
                    "help:read",
                    "memory:read",
                    "llm_logs:read",
                    "config:read",
                    "prompt:read",
                    "announce:read",
                    "scene:read",
                    "user_ban:read",
                ],
            }
        ],
        api_allowed_origins=[],
    )
    assert register_management_api_for_driver(
        driver=_FakeDriver("fastapi", api_app),
        config=config,
        component_loader=_build_components,
        logger=logger,
    )
    headers = {
        "Authorization": "Bearer read-only-token-000000",
        "X-Komari-Change-Reason": "权限边界验证",
        "X-Request-ID": "read-only-boundary",
    }

    async with app.test_server(asgi=cast("Any", api_app)) as ctx:
        client = ctx.get_client()
        responses = [
            await client.post(
                "/api/komari-knowledge/v1/knowledge",
                headers=headers,
                json={"content": "测试", "keywords": ["测试"]},
            ),
            await client.post(
                "/api/komari-help/v1/help",
                headers=headers,
                json={"title": "测试", "content": "测试"},
            ),
            await client.post(
                "/api/komari-memory/v1/conversations",
                headers=headers,
                json={
                    "group_id": "1",
                    "summary": "测试",
                    "participants": ["1"],
                },
            ),
            await client.patch(
                "/api/komari-management-config/v1/resources/komari_management/fields/plugin_enable",
                headers=headers,
                json={"value": False},
            ),
            await client.patch(
                "/api/komari-management-prompt/v1/resources/komari_chat/fields/system_prompt",
                headers=headers,
                json={"value": "测试"},
            ),
            await client.post(
                "/api/komari-announce/v1/maintenance",
                headers=headers,
                json={
                    "title": "测试",
                    "content": "测试",
                    "scheduled_time": "2026-07-17 03:00",
                    "group_ids": [1],
                },
            ),
            await client.put(
                "/api/komari-decision-scenes/v1/scenes/TEST",
                headers=headers,
                json={"scene_type": "general", "content_text": "测试"},
            ),
            await client.post(
                "/api/komari-user-bans/v1/bans",
                headers=headers,
                json={"user_id": "10086", "scope": "chat"},
            ),
        ]

    assert [response.status_code for response in responses] == [403] * len(responses)


def test_register_management_api_skips_disabled_config() -> None:
    logger = _FakeLogger()

    registered = register_management_api_for_driver(
        driver=_FakeDriver("fastapi", _build_api_app()),
        config=SimpleNamespace(
            plugin_enable=False,
            api_token="secret-token-00000000",
            api_allowed_origins=[],
        ),
        component_loader=_build_components,
        logger=logger,
    )

    assert registered is False
    assert logger.info_messages == ["[Komari Management] 插件未启用，跳过管理 API 注册"]


def test_register_management_api_skips_missing_token_and_non_fastapi() -> None:
    logger = _FakeLogger()

    registered_missing_token = register_management_api_for_driver(
        driver=_FakeDriver("fastapi", _build_api_app()),
        config=SimpleNamespace(
            plugin_enable=True,
            api_token="",
            api_allowed_origins=[],
        ),
        component_loader=_build_components,
        logger=logger,
    )
    assert registered_missing_token is False
    assert "未配置有效管理凭据" in logger.warning_messages[0]

    non_fastapi_logger = _FakeLogger()
    registered_non_fastapi = register_management_api_for_driver(
        driver=_FakeDriver("aiohttp"),
        config=SimpleNamespace(
            plugin_enable=True,
            api_token="secret-token-00000000",
            api_allowed_origins=[],
        ),
        component_loader=_build_components,
        logger=non_fastapi_logger,
    )
    assert registered_non_fastapi is False
    assert any(
        "当前驱动不是 FastAPI" in message
        for message in non_fastapi_logger.warning_messages
    )
