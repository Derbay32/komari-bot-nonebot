"""LLM Provider API 挂载测试。"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI

from komari_bot.common.management_api import SharedManagementSettings
from komari_bot.plugins.llm_provider import api_runtime as api_runtime_module
from komari_bot.plugins.llm_provider.api import API_PREFIX


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


def test_register_management_api_for_fastapi_driver(monkeypatch: object) -> None:
    app = FastAPI()
    logger = _FakeLogger()
    config = SimpleNamespace(plugin_enable=True, api_enabled=True)
    monkeypatch.setattr(
        api_runtime_module,
        "load_shared_management_settings",
        lambda _logger: SharedManagementSettings(
            api_token="secret-token",
            allowed_origins=("https://ui.example.com",),
        ),
    )

    registered = api_runtime_module.register_management_api_for_driver(
        driver=_FakeDriver("fastapi", app),
        config=config,
        reader_getter=lambda: SimpleNamespace(),
        logger=logger,
    )

    route_paths = {getattr(route, "path", "") for route in app.routes}
    assert registered is True
    assert f"{API_PREFIX}/reply-logs" in route_paths
    assert any(
        getattr(middleware.cls, "__name__", "") == "CORSMiddleware"
        for middleware in app.user_middleware
    )


def test_register_management_api_skips_disabled_config() -> None:
    logger = _FakeLogger()

    registered = api_runtime_module.register_management_api_for_driver(
        driver=_FakeDriver("fastapi", FastAPI()),
        config=SimpleNamespace(plugin_enable=True, api_enabled=False),
        reader_getter=lambda: SimpleNamespace(),
        logger=logger,
    )

    assert registered is False
    assert "本地 REST 管理接口已禁用" in logger.info_messages[0]


def test_register_management_api_skips_missing_shared_settings_and_non_fastapi(
    monkeypatch: object,
) -> None:
    logger = _FakeLogger()
    monkeypatch.setattr(
        api_runtime_module,
        "load_shared_management_settings",
        lambda _logger: None,
    )

    missing_settings = api_runtime_module.register_management_api_for_driver(
        driver=_FakeDriver("fastapi", FastAPI()),
        config=SimpleNamespace(plugin_enable=True, api_enabled=True),
        reader_getter=lambda: SimpleNamespace(),
        logger=logger,
    )
    assert missing_settings is False

    non_fastapi_logger = _FakeLogger()
    monkeypatch.setattr(
        api_runtime_module,
        "load_shared_management_settings",
        lambda _logger: SharedManagementSettings(
            api_token="secret-token",
            allowed_origins=(),
        ),
    )
    non_fastapi = api_runtime_module.register_management_api_for_driver(
        driver=_FakeDriver("aiohttp"),
        config=SimpleNamespace(plugin_enable=True, api_enabled=True),
        reader_getter=lambda: SimpleNamespace(),
        logger=non_fastapi_logger,
    )
    assert non_fastapi is False
    assert "当前驱动不是 FastAPI" in non_fastapi_logger.warning_messages[0]
