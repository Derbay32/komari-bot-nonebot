"""Komari Healthcheck 插件测试。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi import FastAPI

from komari_bot.plugins.komari_healthcheck import (
    HealthCheckConfig,
    register_healthcheck_route,
)

if TYPE_CHECKING:
    from nonebug import App


def _build_app(config: HealthCheckConfig | None = None) -> FastAPI:
    api_app = FastAPI()
    register_healthcheck_route(api_app, config or HealthCheckConfig())
    return api_app


@pytest.mark.asyncio
async def test_healthcheck_returns_200_when_bot_online(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "komari_bot.plugins.komari_healthcheck.get_bots",
        lambda: {"bot-1": object()},
    )

    async with app.test_server(asgi=cast("Any", _build_app())) as ctx:
        client = ctx.get_client()
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.text == "OK"
    assert response.headers["content-type"].startswith("text/plain")


@pytest.mark.asyncio
async def test_healthcheck_returns_503_when_bot_offline(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("komari_bot.plugins.komari_healthcheck.get_bots", dict)

    config = HealthCheckConfig(
        endpoint_path="healthz/",
        online_message="ONLINE",
        offline_message="OFFLINE",
    )
    async with app.test_server(asgi=cast("Any", _build_app(config))) as ctx:
        client = ctx.get_client()
        response = await client.get("/healthz")

    assert response.status_code == 503
    assert response.text == "OFFLINE"


def test_register_healthcheck_route_is_idempotent() -> None:
    api_app = FastAPI()
    config = HealthCheckConfig()

    first_registered = register_healthcheck_route(api_app, config)
    second_registered = register_healthcheck_route(api_app, config)

    route_paths = [getattr(route, "path", None) for route in api_app.routes]
    assert first_registered is True
    assert second_registered is False
    assert route_paths.count("/health") == 1


def test_healthcheck_config_normalizes_endpoint_path() -> None:
    config = HealthCheckConfig(endpoint_path=" healthz/ ")

    assert config.endpoint_path == "/healthz"
