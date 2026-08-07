"""komari_custom 启动守卫测试（ticket 11：守卫改为 SQLALCHEMY_DATABASE_URL 预检）。"""

from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import nonebot.plugin
import pytest

if TYPE_CHECKING:
    from nonebug import App

PACKAGE_NAME = "komari_bot.plugins.komari_custom"


@pytest.fixture
def custom_module(app: App, monkeypatch: pytest.MonkeyPatch) -> Any:
    del app
    original_require = nonebot.plugin.require

    def _require(plugin_name: str) -> object:
        if plugin_name == "nonebot_plugin_apscheduler":
            return SimpleNamespace()
        if plugin_name == "komari_knowledge":
            return SimpleNamespace()
        return original_require(plugin_name)

    monkeypatch.setattr(nonebot.plugin, "require", _require)

    shim = sys.modules.pop(PACKAGE_NAME, None)
    module = importlib.import_module(PACKAGE_NAME)
    if shim is not None:
        monkeypatch.setitem(sys.modules, PACKAGE_NAME, shim)

    async def _dummy_config() -> SimpleNamespace:
        return SimpleNamespace(plugin_enable=True)

    monkeypatch.setattr(module.config_manager, "get_async", _dummy_config)
    return module


@pytest.mark.asyncio
async def test_unconfigured_database_url_skips_repository_initialization(
    custom_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize_called = False

    async def _initialize() -> None:
        nonlocal initialize_called
        initialize_called = True

    monkeypatch.setattr(custom_module, "is_orm_database_url_configured", lambda: False)
    monkeypatch.setattr(custom_module.repository, "initialize", _initialize)

    await custom_module.on_startup()

    assert initialize_called is False


@pytest.mark.asyncio
async def test_configured_database_url_proceeds_to_repository_initialization(
    custom_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize_called = False

    async def _initialize() -> None:
        nonlocal initialize_called
        initialize_called = True

    monkeypatch.setattr(custom_module, "is_orm_database_url_configured", lambda: True)
    monkeypatch.setattr(custom_module.repository, "initialize", _initialize)
    monkeypatch.setattr(custom_module.repository, "cleanup_expired", _initialize)

    await custom_module.on_startup()

    assert initialize_called is True
