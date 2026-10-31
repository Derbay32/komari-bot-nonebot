"""TSK-193 Hindsight caller plugin lifecycle acceptance."""

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
    saved_service = sys.modules.get(f"{PACKAGE}.services.hindsight_service")
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
        if saved_service is None:
            sys.modules.pop(f"{PACKAGE}.services.hindsight_service", None)
        else:
            sys.modules[f"{PACKAGE}.services.hindsight_service"] = saved_service


async def test_plugin_public_surface_is_hindsight_only() -> None:
    async with _loaded_memory_plugin() as module:
        assert set(module.__all__) == {
            "HindsightMemoryService",
            "HindsightRecallSession",
            "HindsightRetainRequest",
            "HindsightUnavailableError",
            "KomariMemoryConfigSchema",
            "get_hindsight_memory",
        }
        for removed in (
            "MemoryService",
            "get_memory_service",
            "get_plugin_manager",
            "get_redis_manager",
            "register_memory_api",
            "register_summary_task",
            "register_interaction_event_task",
            "register_forgetting_task",
            "create_pool",
        ):
            assert not hasattr(module, removed), removed
        assert module.get_hindsight_memory() is None


@pytest.mark.asyncio
async def test_startup_keeps_stable_service_when_config_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _loaded_memory_plugin() as module:

        async def _config() -> object:
            raise RuntimeError("config storage unavailable")  # noqa: TRY003

        monkeypatch.setattr(module, "get_config_async", _config)

        await module.startup()

        service = module.get_hindsight_memory()
        assert service is not None
        with pytest.raises(module.HindsightUnavailableError) as captured:
            async with service.open_session():
                pass
        assert captured.value.reason_code == "configuration_unavailable"
        assert service._ready is False

        await module.shutdown()
        assert module.get_hindsight_memory() is None


@pytest.mark.asyncio
async def test_startup_keeps_stable_service_when_memory_config_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0018：没有停用开关后，缺失的连接配置同样只让记忆不可用。"""
    async with _loaded_memory_plugin() as module:

        async def _config() -> KomariMemoryConfigSchema:
            return KomariMemoryConfigSchema()

        monkeypatch.setattr(module, "get_config_async", _config)

        await module.startup()

        service = module.get_hindsight_memory()
        assert service is not None
        with pytest.raises(module.HindsightUnavailableError) as captured:
            async with service.open_session():
                pass
        assert captured.value.reason_code == "configuration_incomplete"
        await module.shutdown()


@pytest.mark.asyncio
async def test_startup_probe_failure_is_isolated_and_shutdown_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _loaded_memory_plugin() as module:
        probes: list[int] = []
        closes: list[int] = []

        class _FakeService:
            def __init__(self, *, config_getter: object) -> None:
                assert callable(config_getter)

            async def startup_probe(self) -> None:
                probes.append(1)
                raise RuntimeError("probe transport exploded")  # noqa: TRY003

            async def close(self) -> None:
                closes.append(1)

        monkeypatch.setattr(module, "HindsightMemoryService", _FakeService)

        await module.startup()
        assert probes == [1]
        assert isinstance(module.get_hindsight_memory(), _FakeService)

        await module.shutdown()
        await module.shutdown()
        assert closes == [1]
        assert module.get_hindsight_memory() is None


@pytest.mark.asyncio
async def test_shutdown_closes_owned_service_without_shared_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _loaded_memory_plugin() as module:
        closed: list[str] = []
        shared_redis = SimpleNamespace(closed=False)

        class _FakeService:
            def __init__(self, *, config_getter: object) -> None:
                del config_getter

            async def startup_probe(self) -> None:
                return None

            async def close(self) -> None:
                closed.append("service")

        monkeypatch.setattr(module, "HindsightMemoryService", _FakeService)
        await module.startup()
        await module.shutdown()

        assert closed == ["service"]
        assert shared_redis.closed is False
