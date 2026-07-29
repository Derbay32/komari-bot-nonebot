"""Komari Search 提供者描述 API 测试。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi import FastAPI
from pydantic import Field

from komari_bot.plugins.komari_search import api as search_api
from komari_bot.plugins.komari_search.config_schema import DynamicConfigSchema

if TYPE_CHECKING:
    from nonebug import App
    from pytest import MonkeyPatch


def _build_app(
    *,
    permissions: list[str] | None = None,
) -> FastAPI:
    api_app = FastAPI()
    search_api.register_search_api(
        api_app,
        api_token=[
            {
                "credential_id": "search-dashboard",
                "token": "search-token-00000000",
                "permissions": permissions or ["search:read"],
            }
        ],
        allowed_origins=["https://ui.example.com"],
    )
    return api_app


@pytest.mark.asyncio
async def test_provider_descriptors_group_schema_fields_and_protect_secrets(
    app: App,
) -> None:
    headers = {"Authorization": "Bearer search-token-00000000"}
    async with app.test_server(asgi=cast("Any", _build_app())) as ctx:
        response = await ctx.get_client().get(
            f"{search_api.API_PREFIX}/provider-descriptors",
            headers=headers,
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["current_provider"] == "tavily"
    assert payload["available_providers"] == ["tavily", "exa"]
    common = {item["field_name"]: item for item in payload["common_fields"]}
    assert "version" not in common
    assert common["search_api_key"]["secret"] is True
    assert common["max_results"]["field_type"] == "int"
    providers = {
        item["provider_id"]: {field["field_name"] for field in item["fields"]}
        for item in payload["providers"]
    }
    assert providers["tavily"] == {
        "tavily_search_depth",
        "tavily_include_answer",
    }
    assert providers["exa"] == {"exa_search_type", "exa_fetch_format"}


@pytest.mark.asyncio
async def test_provider_descriptors_reflect_new_schema_fields(
    app: App,
    monkeypatch: MonkeyPatch,
) -> None:
    class _ExtendedSearchConfig(DynamicConfigSchema):
        tavily_result_mode: str = Field(
            default="compact",
            description="Tavily 结果展示模式",
        )

    monkeypatch.setattr(search_api, "DynamicConfigSchema", _ExtendedSearchConfig)
    async with app.test_server(asgi=cast("Any", _build_app())) as ctx:
        response = await ctx.get_client().get(
            f"{search_api.API_PREFIX}/provider-descriptors",
            headers={"Authorization": "Bearer search-token-00000000"},
        )

    assert response.status_code == 200
    tavily = next(
        item
        for item in response.json()["providers"]
        if item["provider_id"] == "tavily"
    )
    reflected = next(
        item
        for item in tavily["fields"]
        if item["field_name"] == "tavily_result_mode"
    )
    assert reflected == {
        "field_name": "tavily_result_mode",
        "field_type": "str",
        "description": "Tavily 结果展示模式",
        "default": "compact",
        "secret": False,
    }


@pytest.mark.asyncio
async def test_provider_descriptors_require_search_read_permission(app: App) -> None:
    path = f"{search_api.API_PREFIX}/provider-descriptors"
    async with app.test_server(
        asgi=cast("Any", _build_app(permissions=["config:read"]))
    ) as ctx:
        client = ctx.get_client()
        unauthorized = await client.get(path)
        forbidden = await client.get(
            path,
            headers={"Authorization": "Bearer search-token-00000000"},
        )

    assert unauthorized.status_code == 401
    assert forbidden.status_code == 403
