"""Komari Management 与 NoneBot FastAPI 驱动集成测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import nonebot
import pytest
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
from komari_bot.plugins.komari_search.api import register_search_api
from komari_bot.plugins.user_ban.api import register_user_ban_api

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


class _DummyConfigModel(BaseModel):
    plugin_enable: bool = True


class _DummyConfigManager:
    @property
    def config_source(self) -> str:
        return "postgres:komari_plugin_configs/komari_management"

    async def get_async(self) -> BaseModel:
        return _DummyConfigModel()

    async def update_field_async(
        self, field_name: str, value: object
    ) -> BaseModel:
        del field_name, value
        return await self.get_async()

    async def reload_async(self) -> BaseModel:
        return await self.get_async()


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
        register_search_api=register_search_api,
        register_user_ban_api=register_user_ban_api,
        user_ban_service_getter=lambda: None,
        config_resources=(
            ManagedConfigResource(
                resource_id="komari_management",
                display_name="Komari Management",
                manager_getter=lambda: _DummyConfigManager(),
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
async def test_nonebot_fastapi_driver_exposes_docs_and_management_routes(
    app: App,
) -> None:
    driver = nonebot.get_driver()
    logger = _FakeLogger()

    registered = register_management_api_for_driver(
        driver=driver,
        config=SimpleNamespace(
            plugin_enable=True,
            api_credentials=[
                {
                    "credential_id": "test-operator",
                    "token": "secret-token-00000000",
                    "permissions": ["*"],
                }
            ],
            api_allowed_origins=[],
        ),
        component_loader=_build_components,
        logger=logger,
    )

    assert registered is True

    async with app.test_server() as ctx:
        client = ctx.get_client()
        docs = await client.get("/api/docs")
        schema_response = await client.get("/api/openapi.json")

    assert docs.status_code == 200
    assert schema_response.status_code == 200

    schema = schema_response.json()
    assert "/api/v2/komari-knowledge/knowledge" in schema["paths"]
    assert "/api/v2/komari-help/help" in schema["paths"]
    assert "/api/v2/komari-memory/conversations" in schema["paths"]
    assert "/api/v2/agent-run-logs/runs" in schema["paths"]
    assert "/api/v2/komari-search/provider-descriptors" in schema["paths"]
    assert "/api/v2/komari-management-config/resources" in schema["paths"]
    assert "/api/v2/komari-management-prompt/resources" in schema["paths"]
    assert "/api/v2/komari-user-bans/bans" in schema["paths"]
    assert "/api/llm-provider/v1/reply-logs" not in schema["paths"]
    tag_names = {
        tag
        for operations in schema["paths"].values()
        for operation in operations.values()
        for tag in operation.get("tags", [])
    }
    assert {
        "komari-knowledge",
        "komari-help",
        "komari-memory",
        "agent-run-logs",
        "komari-search",
        "komari-management-config",
        "komari-management-prompt",
        "komari-user-bans",
    } <= tag_names
