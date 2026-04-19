"""Komari Management 与 NoneBot FastAPI 驱动集成测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import nonebot
import pytest
from pydantic import BaseModel

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


class _DummyConfigModel(BaseModel):
    plugin_enable: bool = True


class _DummyConfigManager:
    @property
    def config_file(self) -> Path:
        return Path("/tmp/komari_management.json")

    def get(self) -> BaseModel:
        return _DummyConfigModel()

    def update_field(self, field_name: str, value: object) -> BaseModel:
        del field_name, value
        return self.get()

    def reload_from_json(self) -> BaseModel:
        return self.get()


def _build_components() -> ManagementApiComponents:
    return ManagementApiComponents(
        register_knowledge_api=register_knowledge_api,
        knowledge_engine_getter=lambda: None,
        register_help_api=register_help_api,
        help_engine_getter=lambda: None,
        register_memory_api=register_memory_api,
        memory_service_getter=lambda: None,
        register_llm_provider_api=register_llm_provider_api,
        reply_log_reader_getter=lambda: None,
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
                file_path=Path("config") / "prompts" / "komari_memory.yaml",
                defaults={"system_prompt": "默认值"},
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
    assert "/api/komari-help/v1/help" in schema["paths"]
    assert "/api/komari-memory/v1/conversations" in schema["paths"]
    assert "/api/llm-provider/v1/reply-logs" in schema["paths"]
    assert "/api/komari-management-config/v1/resources" in schema["paths"]
    assert "/api/komari-management-prompt/v1/resources" in schema["paths"]
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
        "llm-provider",
        "komari-management-config",
        "komari-management-prompt",
    } <= tag_names
