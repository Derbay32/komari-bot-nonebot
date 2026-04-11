"""Komari Management 与 NoneBot FastAPI 驱动集成测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import nonebot
import pytest

from komari_bot.plugins.komari_knowledge.api import register_knowledge_api
from komari_bot.plugins.komari_management.api_runtime import (
    ManagementApiComponents,
    register_management_api_for_driver,
)
from komari_bot.plugins.komari_memory.api import register_memory_api
from komari_bot.plugins.llm_provider.api import register_llm_provider_api

if TYPE_CHECKING:
    from nonebug import App


class _FakeLogger:
    def __init__(self) -> None:
        self.info_messages: list[str] = []
        self.warning_messages: list[str] = []

    def info(self, message: str, *args: object) -> None:
        self.info_messages.append(message % args if args else message)

    def warning(self, message: str, *args: object) -> None:
        self.warning_messages.append(message % args if args else message)


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
async def test_nonebot_fastapi_driver_exposes_docs_and_management_routes(
    app: App,
) -> None:
    driver = nonebot.get_driver()
    logger = _FakeLogger()

    registered = register_management_api_for_driver(
        driver=driver,
        config=SimpleNamespace(
            plugin_enable=True,
            api_token="secret-token",
            api_allowed_origins=[],
        ),
        component_loader=_build_components,
        logger=logger,
    )

    assert registered is True

    async with app.test_server() as ctx:
        client = ctx.get_client()
        docs = await client.get("/api/komari-management/docs")
        schema_response = await client.get("/api/komari-management/openapi.json")

    assert docs.status_code == 200
    assert schema_response.status_code == 200

    schema = schema_response.json()
    assert "/api/komari-knowledge/v1/knowledge" in schema["paths"]
    assert "/api/komari-memory/v1/conversations" in schema["paths"]
    assert "/api/llm-provider/v1/reply-logs" in schema["paths"]
    tag_names = {
        tag
        for operations in schema["paths"].values()
        for operation in operations.values()
        for tag in operation.get("tags", [])
    }
    assert {"komari-knowledge", "komari-memory", "llm-provider"} <= tag_names
