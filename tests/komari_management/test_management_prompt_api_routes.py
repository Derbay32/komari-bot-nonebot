"""Komari Management Prompt 接口路由测试。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi import FastAPI

from komari_bot.plugins.komari_management.managed_resources import ManagedPromptResource
from komari_bot.plugins.komari_management.prompt_api import (
    API_PREFIX,
    register_prompt_api,
)

if TYPE_CHECKING:
    from pathlib import Path

    from nonebug import App


def _build_app(prompt_file: Path) -> FastAPI:
    api_app = FastAPI()
    register_prompt_api(
        api_app,
        api_token="secret-token",
        allowed_origins=["https://ui.example.com"],
        resources=(
            ManagedPromptResource(
                resource_id="komari_chat",
                display_name="Komari Chat Prompt",
                file_path=prompt_file,
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
    tmp_path: Path,
) -> None:
    prompt_file = tmp_path / "prompt.yaml"
    prompt_file.write_text("system_prompt: 你好\nmemory_ack: 收到\n", encoding="utf-8")

    async with app.test_server(asgi=cast("Any", _build_app(prompt_file))) as ctx:
        client = ctx.get_client()
        unauthorized = await client.get(f"{API_PREFIX}/resources")
        listed = await client.get(
            f"{API_PREFIX}/resources",
            headers={"Authorization": "Bearer secret-token"},
        )

    assert unauthorized.status_code == 401
    assert listed.status_code == 200
    assert listed.json()["items"][0]["resource_id"] == "komari_chat"


@pytest.mark.asyncio
async def test_prompt_routes_support_detail_replace_and_field_update(
    app: App,
    tmp_path: Path,
) -> None:
    prompt_file = tmp_path / "prompt.yaml"
    prompt_file.write_text("system_prompt: 你好\nmemory_ack: 收到\n", encoding="utf-8")
    headers = {"Authorization": "Bearer secret-token"}

    async with app.test_server(asgi=cast("Any", _build_app(prompt_file))) as ctx:
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
    tmp_path: Path,
) -> None:
    prompt_file = tmp_path / "prompt.yaml"
    prompt_file.write_text("system_prompt: 你好\nmemory_ack: 收到\n", encoding="utf-8")
    headers = {"Authorization": "Bearer secret-token"}

    async with app.test_server(asgi=cast("Any", _build_app(prompt_file))) as ctx:
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
