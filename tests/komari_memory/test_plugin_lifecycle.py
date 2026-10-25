"""TSK-192 phase A: memory lifecycle borrows context resources safely."""

from __future__ import annotations

import importlib.util
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from komari_bot.plugins.komari_memory.config_schema import (
    KomariMemoryConfigSchema,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = "komari_bot.plugins.komari_memory"
PLUGIN_DIR = PROJECT_ROOT / "komari_bot" / "plugins" / "komari_memory"


@asynccontextmanager
async def _loaded_memory_plugin() -> AsyncIterator[Any]:
    from nonebot import get_driver

    lifespan = get_driver()._lifespan
    startup_before = list(lifespan._startup_funcs)
    shutdown_before = list(lifespan._shutdown_funcs)
    saved_package = sys.modules.get(PACKAGE)
    saved_schema = sys.modules.get(f"{PACKAGE}.config_schema")
    spec = importlib.util.spec_from_file_location(
        PACKAGE,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        lifespan._startup_funcs[:] = startup_before
        lifespan._shutdown_funcs[:] = shutdown_before
        if saved_package is None:
            sys.modules.pop(PACKAGE, None)
        else:
            sys.modules[PACKAGE] = saved_package
        if saved_schema is None:
            sys.modules.pop(f"{PACKAGE}.config_schema", None)
        else:
            sys.modules[f"{PACKAGE}.config_schema"] = saved_schema


@pytest.mark.asyncio
async def test_memory_startup_failure_does_not_raise_or_close_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _loaded_memory_plugin() as module:
        class _ContextRedis:
            def __init__(self) -> None:
                self.closed = False

        context_redis = _ContextRedis()
        context_store = SimpleNamespace(redis=context_redis)

        async def _config() -> KomariMemoryConfigSchema:
            return KomariMemoryConfigSchema(plugin_enable=True)

        async def _create_pool() -> object:
            msg = "postgres unavailable"
            raise RuntimeError(msg)

        monkeypatch.setattr(module, "get_config_async", _config)
        monkeypatch.setattr(module, "get_context_store", lambda: context_store)
        monkeypatch.setattr(module, "create_pool", _create_pool)

        assert module.get_memory_service() is None
        await module.startup()

        assert module.get_memory_service() is None
        assert module.get_redis_manager() is None
        assert module.get_plugin_manager() is None
        assert context_redis.closed is False


@pytest.mark.asyncio
async def test_memory_config_read_failure_does_not_abort_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _loaded_memory_plugin() as module:
        async def _config() -> KomariMemoryConfigSchema:
            msg = "config storage unavailable"
            raise RuntimeError(msg)

        monkeypatch.setattr(module, "get_config_async", _config)
        await module.startup()
        assert module.get_plugin_manager() is None


@pytest.mark.asyncio
async def test_memory_shutdown_drops_references_without_closing_shared_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _loaded_memory_plugin() as module:
        class _ContextRedis:
            def __init__(self) -> None:
                self.closed = 0

            async def aclose(self) -> None:
                self.closed += 1

        context_redis = _ContextRedis()
        closed_pools: list[str] = []

        async def _close_pool() -> None:
            closed_pools.append("closed")

        manager = module.PluginManager(KomariMemoryConfigSchema())
        manager.redis = SimpleNamespace(redis=context_redis)
        manager.memory = SimpleNamespace(cleanup=lambda: _noop())
        manager.forgetting = object()
        manager.pg_pool = SimpleNamespace(close=_close_pool)
        monkeypatch.setattr(module, "_plugin_manager", manager)
        monkeypatch.setattr(module, "unregister_summary_task", lambda: None)
        monkeypatch.setattr(module, "unregister_forgetting_task", lambda: None)
        monkeypatch.setattr(module, "unregister_interaction_event_task", lambda: None)

        await module.shutdown()

        assert manager.redis is None
        assert manager.memory is None
        assert manager.forgetting is None
        assert manager.pg_pool is None
        assert context_redis.closed == 0
        assert closed_pools == []
        assert module.get_memory_service() is None
        assert module.get_redis_manager() is None


async def _noop() -> None:
    return None
