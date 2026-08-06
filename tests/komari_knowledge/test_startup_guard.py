"""komari_knowledge 启动守卫测试（ticket 11：守卫改为 SQLALCHEMY_DATABASE_URL 预检）。"""

from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import nonebot.plugin
import pytest

if TYPE_CHECKING:
    from nonebug import App

PACKAGE_NAME = "komari_bot.plugins.komari_knowledge"


@pytest.fixture
def knowledge_module(app: App, monkeypatch: pytest.MonkeyPatch) -> Any:
    del app
    original_require = nonebot.plugin.require

    def _require(plugin_name: str) -> object:
        if plugin_name == "embedding_provider":
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
async def test_unconfigured_database_url_skips_engine_initialization(
    knowledge_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    async def _initialize() -> object:
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(knowledge_module, "is_orm_database_url_configured", lambda: False)
    monkeypatch.setattr(knowledge_module, "initialize_engine", _initialize)

    await knowledge_module.on_startup()

    assert called is False


@pytest.mark.asyncio
async def test_configured_database_url_proceeds_to_engine_initialization(
    knowledge_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    async def _initialize() -> object:
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(knowledge_module, "is_orm_database_url_configured", lambda: True)
    monkeypatch.setattr(knowledge_module, "initialize_engine", _initialize)

    await knowledge_module.on_startup()

    assert called is True
