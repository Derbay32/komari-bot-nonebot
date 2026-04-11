"""Komari Management API 挂载测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI

from komari_bot.plugins.komari_knowledge.api import register_knowledge_api
from komari_bot.plugins.komari_management.api_runtime import (
    ManagementApiComponents,
    register_management_api_for_driver,
)
from komari_bot.plugins.komari_memory.api import register_memory_api
from komari_bot.plugins.llm_provider.api import register_llm_provider_api

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

    def info(self, message: str, *args: object) -> None:
        self.info_messages.append(message % args if args else message)

    def warning(self, message: str, *args: object) -> None:
        self.warning_messages.append(message % args if args else message)


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
        register_memory_api=register_memory_api,
        memory_service_getter=lambda: None,
        register_llm_provider_api=register_llm_provider_api,
        reply_log_reader_getter=lambda: None,
    )


@pytest.mark.asyncio
async def test_register_management_api_for_fastapi_driver(app: App) -> None:
    api_app = _build_api_app()
    logger = _FakeLogger()
    config = SimpleNamespace(
        plugin_enable=True,
        api_token="secret-token",
        api_allowed_origins=["https://ui.example.com"],
    )

    registered = register_management_api_for_driver(
        driver=_FakeDriver("fastapi", api_app),
        config=config,
        component_loader=_build_components,
        logger=logger,
    )

    route_paths = {getattr(route, "path", "") for route in api_app.routes}
    assert registered is True
    assert "/api/komari-knowledge/v1/knowledge" in route_paths
    assert "/api/komari-memory/v1/conversations" in route_paths
    assert "/api/llm-provider/v1/reply-logs" in route_paths

    async with app.test_server(asgi=api_app) as ctx:
        client = ctx.get_client()
        docs = await client.get("/api/komari-management/docs")
        schema_response = await client.get("/api/komari-management/openapi.json")

    assert docs.status_code == 200
    assert schema_response.status_code == 200
    schema = schema_response.json()
    assert "/api/komari-knowledge/v1/knowledge" in schema["paths"]
    assert "/api/komari-memory/v1/conversations" in schema["paths"]
    assert "/api/llm-provider/v1/reply-logs" in schema["paths"]
    security_schemes = schema["components"]["securitySchemes"]
    assert any(
        item.get("type") == "http" and item.get("scheme") == "bearer"
        for item in security_schemes.values()
    )
    assert logger.warning_messages == []


def test_register_management_api_skips_disabled_config() -> None:
    logger = _FakeLogger()

    registered = register_management_api_for_driver(
        driver=_FakeDriver("fastapi", _build_api_app()),
        config=SimpleNamespace(
            plugin_enable=False,
            api_token="secret-token",
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
    assert "未配置 api_token" in logger.warning_messages[0]

    non_fastapi_logger = _FakeLogger()
    registered_non_fastapi = register_management_api_for_driver(
        driver=_FakeDriver("aiohttp"),
        config=SimpleNamespace(
            plugin_enable=True,
            api_token="secret-token",
            api_allowed_origins=[],
        ),
        component_loader=_build_components,
        logger=non_fastapi_logger,
    )
    assert registered_non_fastapi is False
    assert "当前驱动不是 FastAPI" in non_fastapi_logger.warning_messages[0]
