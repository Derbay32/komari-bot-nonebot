"""KomariMemory PluginManager startup chain tests."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import nonebot
import nonebot.plugin

if TYPE_CHECKING:
    from collections.abc import Callable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_PATH = PROJECT_ROOT / "komari_bot/plugins/komari_memory/__init__.py"


class _FakeDriver:
    def on_startup(
        self,
        func: Callable[..., object] | None = None,
    ) -> Callable[..., object]:
        def _decorator(handler: Callable[..., object]) -> Callable[..., object]:
            return handler

        return _decorator(func) if func is not None else _decorator

    def on_shutdown(
        self,
        func: Callable[..., object] | None = None,
    ) -> Callable[..., object]:
        def _decorator(handler: Callable[..., object]) -> Callable[..., object]:
            return handler

        return _decorator(func) if func is not None else _decorator


def _load_memory_plugin_module(monkeypatch: Any) -> Any:
    def _fake_require(name: str) -> object:
        if name == "config_manager":
            return SimpleNamespace(
                get_config_manager=lambda _plugin, _schema: SimpleNamespace(
                    get=lambda: SimpleNamespace()
                )
            )
        if name == "character_binding":
            return SimpleNamespace(
                get_character_name=lambda user_id, fallback_nickname="": (
                    fallback_nickname or user_id
                )
            )
        if name == "llm_provider":
            return SimpleNamespace(generate_text=lambda **_kwargs: None)
        if name == "embedding_provider":
            return SimpleNamespace(get_embedding_dimension=lambda: 1536)
        return object()

    monkeypatch.setattr(nonebot, "get_driver", lambda: _FakeDriver())
    monkeypatch.setattr(nonebot.plugin, "require", _fake_require)

    spec = importlib.util.spec_from_file_location(
        "komari_bot.plugins.komari_memory",
        PLUGIN_PATH,
        submodule_search_locations=[str(PLUGIN_PATH.parent)],
    )
    if spec is None or spec.loader is None:
        raise AssertionError

    module = importlib.util.module_from_spec(spec)
    original_module = sys.modules.get("komari_bot.plugins.komari_memory")
    sys.modules["komari_bot.plugins.komari_memory"] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if original_module is not None:
            sys.modules["komari_bot.plugins.komari_memory"] = original_module
        else:
            sys.modules.pop("komari_bot.plugins.komari_memory", None)

    return module


class _FakePool:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeRedisManager:
    def __init__(self, config: object, events: list[tuple[str, object]]) -> None:
        del config
        self.events = events
        self.initialized = False
        self.closed = False

    async def initialize(self) -> None:
        self.initialized = True
        self.events.append(("redis_initialize", True))

    async def close(self) -> None:
        self.closed = True
        self.events.append(("redis_close", True))


class _FakeMemoryService:
    def __init__(self, events: list[tuple[str, object]]) -> None:
        self.events = events
        self.cleaned = False

    async def cleanup(self) -> None:
        self.cleaned = True
        self.events.append(("memory_cleanup", True))


def test_initialize_bootstraps_schema_before_registering_tasks(monkeypatch: Any) -> None:
    module = _load_memory_plugin_module(monkeypatch)
    events: list[tuple[str, object]] = []
    fake_pool = _FakePool()

    async def _fake_create_pool(config: object) -> _FakePool:
        del config
        events.append(("create_pool", True))
        return fake_pool

    async def _fake_apply_schema(pg_pool: object, *, statements: tuple[str, ...]) -> None:
        events.append(("apply_schema", pg_pool is fake_pool))
        assert "VECTOR(1536)" in statements[1]

    async def _fake_validate(
        pg_pool: object,
        *,
        table_name: str,
        column_name: str,
        expected_dimension: int | None,
        label: str,
    ) -> None:
        events.append(("validate", expected_dimension))
        assert pg_pool is fake_pool
        assert table_name == "komari_memory_conversations"
        assert column_name == "embedding"
        assert label == "KomariMemory"

    def _fake_redis_manager(config: object) -> _FakeRedisManager:
        return _FakeRedisManager(config, events)

    def _fake_memory_service(
        config: object,
        conversation_repo: object,
        entity_repo: object,
    ) -> _FakeMemoryService:
        del config, conversation_repo, entity_repo
        events.append(("memory_service", True))
        return _FakeMemoryService(events)

    def _fake_forgetting_service(config: object, pg_pool: object) -> SimpleNamespace:
        del config
        events.append(("forgetting_service", pg_pool is fake_pool))
        return SimpleNamespace(pg_pool=pg_pool)

    monkeypatch.setattr(module, "create_pool", _fake_create_pool)
    monkeypatch.setattr(module, "apply_schema_statements", _fake_apply_schema)
    monkeypatch.setattr(module, "ensure_vector_column_dimension", _fake_validate)
    monkeypatch.setattr(module, "RedisManager", _fake_redis_manager)
    monkeypatch.setattr(module, "ConversationRepository", lambda pool: ("conv", pool))
    monkeypatch.setattr(module, "EntityRepository", lambda pool: ("entity", pool))
    monkeypatch.setattr(module, "MemoryService", _fake_memory_service)
    monkeypatch.setattr(module, "ForgettingService", _fake_forgetting_service)
    monkeypatch.setattr(
        module,
        "register_summary_task",
        lambda redis, memory: events.append(
            ("register_summary", redis.initialized and isinstance(memory, _FakeMemoryService))
        ),
    )
    monkeypatch.setattr(
        module,
        "register_forgetting_task",
        lambda forgetting: events.append(
            ("register_forgetting", getattr(forgetting, "pg_pool", None) is fake_pool)
        ),
    )
    manager = module.PluginManager(config=SimpleNamespace())
    monkeypatch.setattr(
        manager,
        "_resolve_expected_embedding_dimension",
        lambda: 1536,
    )

    asyncio.run(manager.initialize())
    asyncio.run(manager.shutdown())

    assert events[:7] == [
        ("create_pool", True),
        ("apply_schema", True),
        ("validate", 1536),
        ("redis_initialize", True),
        ("memory_service", True),
        ("register_summary", True),
        ("forgetting_service", True),
    ]
    assert ("register_forgetting", True) in events
