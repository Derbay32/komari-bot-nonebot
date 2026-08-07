"""komari_help 启动守卫测试（ticket 11：守卫改为 SQLALCHEMY_DATABASE_URL 预检）。"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import nonebot.plugin
import pytest

if TYPE_CHECKING:
    from nonebug import App

PACKAGE_NAME = "komari_bot.plugins.komari_help"


@pytest.fixture
def help_module(app: App, monkeypatch: pytest.MonkeyPatch) -> Any:
    del app
    original_require = nonebot.plugin.require

    def _require(plugin_name: str) -> object:
        if plugin_name == "embedding_provider":
            return SimpleNamespace()
        return original_require(plugin_name)

    monkeypatch.setattr(nonebot.plugin, "require", _require)

    # 其他测试可能在 driver 就绪前以 driver=None 的方式导入过本包（此时
    # 不会注册 on_startup）。reload 在同一模块对象上重新执行入口，既保留
    # 既有的子模块属性引用（供后续字符串路径 monkeypatch 使用），又能在
    # driver 就绪后注册 on_startup。
    module = importlib.reload(importlib.import_module(PACKAGE_NAME))

    async def _dummy_config() -> SimpleNamespace:
        return SimpleNamespace(plugin_enable=True, auto_scan_on_startup=False)

    monkeypatch.setattr(module.config_manager, "get_async", _dummy_config)
    return module


@pytest.mark.asyncio
async def test_unconfigured_database_url_skips_engine_initialization(
    help_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    async def _initialize() -> object:
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(help_module, "is_orm_database_url_configured", lambda: False)
    monkeypatch.setattr(help_module, "initialize_engine", _initialize)

    await help_module.on_startup()

    assert called is False


@pytest.mark.asyncio
async def test_configured_database_url_proceeds_to_engine_initialization(
    help_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    async def _initialize() -> object:
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(help_module, "is_orm_database_url_configured", lambda: True)
    monkeypatch.setattr(help_module, "initialize_engine", _initialize)

    await help_module.on_startup()

    assert called is True
