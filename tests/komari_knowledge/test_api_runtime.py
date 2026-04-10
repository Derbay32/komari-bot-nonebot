"""Komari Knowledge API 挂载测试。"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI

from komari_bot.plugins.komari_knowledge.api import API_PREFIX
from komari_bot.plugins.komari_knowledge.api_runtime import (
    register_management_api_for_driver,
)


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


def test_register_management_api_for_fastapi_driver() -> None:
    app = FastAPI()
    logger = _FakeLogger()
    config = SimpleNamespace(
        plugin_enable=True,
        api_enabled=True,
        api_token="secret-token",
        api_allowed_origins=["https://ui.example.com"],
    )

    registered = register_management_api_for_driver(
        driver=_FakeDriver("fastapi", app),
        config=config,
        engine_getter=lambda: None,
        logger=logger,
    )

    route_paths = {getattr(route, "path", "") for route in app.routes}
    assert registered is True
    assert f"{API_PREFIX}/knowledge" in route_paths
    assert any(
        getattr(middleware.cls, "__name__", "") == "CORSMiddleware"
        for middleware in app.user_middleware
    )
    assert logger.warning_messages == []


def test_register_management_api_skips_disabled_config() -> None:
    app = FastAPI()
    logger = _FakeLogger()
    config = SimpleNamespace(
        plugin_enable=True,
        api_enabled=False,
        api_token="secret-token",
        api_allowed_origins=[],
    )

    registered = register_management_api_for_driver(
        driver=_FakeDriver("fastapi", app),
        config=config,
        engine_getter=lambda: None,
        logger=logger,
    )

    route_paths = {getattr(route, "path", "") for route in app.routes}
    assert registered is False
    assert f"{API_PREFIX}/knowledge" not in route_paths
    assert "REST 管理接口已禁用" in logger.info_messages[0]


def test_register_management_api_skips_missing_token_and_non_fastapi() -> None:
    logger = _FakeLogger()
    app = FastAPI()
    missing_token_config = SimpleNamespace(
        plugin_enable=True,
        api_enabled=True,
        api_token="",
        api_allowed_origins=[],
    )

    registered_missing_token = register_management_api_for_driver(
        driver=_FakeDriver("fastapi", app),
        config=missing_token_config,
        engine_getter=lambda: None,
        logger=logger,
    )
    assert registered_missing_token is False
    assert "未配置 api_token" in logger.warning_messages[0]

    other_logger = _FakeLogger()
    non_fastapi_config = SimpleNamespace(
        plugin_enable=True,
        api_enabled=True,
        api_token="secret-token",
        api_allowed_origins=[],
    )
    registered_non_fastapi = register_management_api_for_driver(
        driver=_FakeDriver("aiohttp"),
        config=non_fastapi_config,
        engine_getter=lambda: None,
        logger=other_logger,
    )

    assert registered_non_fastapi is False
    assert "当前驱动不是 FastAPI" in other_logger.warning_messages[0]
