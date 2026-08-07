"""KomariDecision PluginManager tests."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from typing import Any, cast

import nonebot.plugin

import komari_bot.plugins.komari_decision as decision_plugin
from komari_bot.plugins.komari_decision.services.decision_engine import (
    DecisionEngine,
)
from komari_bot.plugins.komari_decision.services.runtime_state import (
    DecisionRuntimeStatus,
)


class _FakeSceneRepository:
    def __init__(self, pg_pool: object) -> None:
        self.pg_pool = pg_pool

    async def has_any_scene(self) -> bool:
        return True


class _FakeSceneRuntimeService:
    fail_load = False

    def __init__(self, repository: _FakeSceneRepository) -> None:
        self.repository = repository
        self.snapshot: object | None = None

    async def load_active_set_cache(self) -> bool:
        if self.fail_load:
            raise RuntimeError
        self.snapshot = object()
        return True

    def get_scene_candidates(self) -> object | None:
        return self.snapshot


class _FakeSceneSyncService:
    def __init__(self, repository: _FakeSceneRepository) -> None:
        self.repository = repository


class _FakeSceneEmbeddingWorker:
    def __init__(self, repository: _FakeSceneRepository, *, batch_size: int) -> None:
        self.repository = repository
        self.batch_size = batch_size


class _FakeSceneAdminService:
    def __init__(
        self,
        repository: _FakeSceneRepository,
        runtime_service: _FakeSceneRuntimeService,
        embedding_worker: _FakeSceneEmbeddingWorker,
    ) -> None:
        self.repository = repository
        self.runtime_service = runtime_service
        self.embedding_worker = embedding_worker


def _patch_config(
    monkeypatch: Any,
    *,
    plugin_enable: bool,
    scene_persist_enabled: bool,
) -> None:
    config = SimpleNamespace(
        plugin_enable=plugin_enable,
        scene_persist_enabled=scene_persist_enabled,
    )
    monkeypatch.setattr(decision_plugin, "get_config", lambda: config)

    async def _get_config_async() -> object:
        return config

    monkeypatch.setattr(decision_plugin, "get_config_async", _get_config_async)


def test_initialize_cleans_up_when_bootstrap_fails(monkeypatch: Any) -> None:
    manager = decision_plugin.PluginManager()
    memory_module = sys.modules["komari_bot.plugins.komari_memory"]
    calls = {"register": 0, "unregister": 0}

    monkeypatch.setattr(
        nonebot.plugin,
        "require",
        lambda _name: object(),
    )
    monkeypatch.setattr(
        memory_module,
        "get_plugin_manager",
        lambda: SimpleNamespace(pg_pool=object()),
        raising=False,
    )
    monkeypatch.setattr(
        decision_plugin,
        "require",
        lambda name: memory_module if name == "komari_memory" else object(),
    )
    _patch_config(
        monkeypatch,
        plugin_enable=True,
        scene_persist_enabled=True,
    )
    monkeypatch.setattr(
        "komari_bot.plugins.komari_decision.repositories.scene_repository.SceneRepository",
        _FakeSceneRepository,
    )
    monkeypatch.setattr(
        "komari_bot.plugins.komari_decision.services.scene_runtime_service.SceneRuntimeService",
        _FakeSceneRuntimeService,
    )
    monkeypatch.setattr(
        "komari_bot.plugins.komari_decision.services.scene_sync_service.SceneSyncService",
        _FakeSceneSyncService,
    )
    monkeypatch.setattr(
        "komari_bot.plugins.komari_decision.services.scene_embedding_worker.SceneEmbeddingWorker",
        _FakeSceneEmbeddingWorker,
    )
    monkeypatch.setattr(
        "komari_bot.plugins.komari_decision.services.scene_admin_service.SceneAdminService",
        _FakeSceneAdminService,
    )
    monkeypatch.setattr(
        "komari_bot.plugins.komari_decision.handlers.scene_sync_worker.register_scene_sync_task",
        lambda *_args: calls.__setitem__("register", calls["register"] + 1),
    )

    async def _raise_bootstrap() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "komari_bot.plugins.komari_decision.handlers.scene_sync_worker.bootstrap_scene_sync_task",
        _raise_bootstrap,
    )
    monkeypatch.setattr(
        "komari_bot.plugins.komari_decision.handlers.scene_sync_worker.unregister_scene_sync_task",
        lambda: calls.__setitem__("unregister", calls["unregister"] + 1),
    )

    asyncio.run(manager.initialize())

    assert calls == {"register": 1, "unregister": 1}
    assert manager.scene_repository is None
    assert manager.scene_admin is None
    assert manager.scene_runtime is None
    assert manager.scene_sync is None
    assert manager.scene_embedding_worker is None
    assert manager.runtime_state.status is DecisionRuntimeStatus.FAILED


def test_initialize_recovers_active_cache_during_bootstrap(
    monkeypatch: Any,
) -> None:
    manager = decision_plugin.PluginManager()
    memory_module = sys.modules["komari_bot.plugins.komari_memory"]
    calls = {"register": 0, "bootstrap": 0, "unregister": 0}
    registered_runtime: list[_FakeSceneRuntimeService] = []
    _FakeSceneRuntimeService.fail_load = True

    monkeypatch.setattr(
        nonebot.plugin,
        "require",
        lambda _name: object(),
    )
    monkeypatch.setattr(
        memory_module,
        "get_plugin_manager",
        lambda: SimpleNamespace(pg_pool=object()),
        raising=False,
    )
    monkeypatch.setattr(
        decision_plugin,
        "require",
        lambda name: memory_module if name == "komari_memory" else object(),
    )
    _patch_config(
        monkeypatch,
        plugin_enable=True,
        scene_persist_enabled=True,
    )
    monkeypatch.setattr(
        "komari_bot.plugins.komari_decision.repositories.scene_repository.SceneRepository",
        _FakeSceneRepository,
    )
    monkeypatch.setattr(
        "komari_bot.plugins.komari_decision.services.scene_runtime_service.SceneRuntimeService",
        _FakeSceneRuntimeService,
    )
    monkeypatch.setattr(
        "komari_bot.plugins.komari_decision.services.scene_sync_service.SceneSyncService",
        _FakeSceneSyncService,
    )
    monkeypatch.setattr(
        "komari_bot.plugins.komari_decision.services.scene_embedding_worker.SceneEmbeddingWorker",
        _FakeSceneEmbeddingWorker,
    )
    monkeypatch.setattr(
        "komari_bot.plugins.komari_decision.services.scene_admin_service.SceneAdminService",
        _FakeSceneAdminService,
    )
    def _register(*args: object) -> None:
        calls["register"] += 1
        runtime = args[-1]
        assert isinstance(runtime, _FakeSceneRuntimeService)
        registered_runtime.append(runtime)

    monkeypatch.setattr(
        "komari_bot.plugins.komari_decision.handlers.scene_sync_worker.register_scene_sync_task",
        _register,
    )

    async def _bootstrap() -> None:
        calls["bootstrap"] += 1
        _FakeSceneRuntimeService.fail_load = False
        await registered_runtime[0].load_active_set_cache()

    monkeypatch.setattr(
        "komari_bot.plugins.komari_decision.handlers.scene_sync_worker.bootstrap_scene_sync_task",
        _bootstrap,
    )
    monkeypatch.setattr(
        "komari_bot.plugins.komari_decision.handlers.scene_sync_worker.unregister_scene_sync_task",
        lambda: calls.__setitem__("unregister", calls["unregister"] + 1),
    )

    try:
        asyncio.run(manager.initialize())
    finally:
        _FakeSceneRuntimeService.fail_load = False

    assert calls == {"register": 1, "bootstrap": 1, "unregister": 0}
    assert manager.scene_repository is not None
    assert manager.scene_admin is not None
    assert manager.scene_runtime is not None
    assert manager.scene_sync is not None
    assert manager.scene_embedding_worker is not None
    assert manager.runtime_state.status is DecisionRuntimeStatus.READY


def test_initialize_marks_plugin_disabled_without_loading_services(
    monkeypatch: Any,
) -> None:
    manager = decision_plugin.PluginManager()
    _patch_config(
        monkeypatch,
        plugin_enable=False,
        scene_persist_enabled=True,
    )

    asyncio.run(manager.initialize())

    assert manager.runtime_state.status is DecisionRuntimeStatus.DISABLED
    assert manager.scene_runtime is None


def test_initialize_marks_scene_persistence_disabled(
    monkeypatch: Any,
) -> None:
    manager = decision_plugin.PluginManager()
    _patch_config(
        monkeypatch,
        plugin_enable=True,
        scene_persist_enabled=False,
    )

    asyncio.run(manager.initialize())

    assert manager.runtime_state.status is DecisionRuntimeStatus.DISABLED
    assert manager.scene_runtime is None


def test_runtime_state_tracks_ready_and_transient_snapshot(
    monkeypatch: Any,
) -> None:
    manager = decision_plugin.PluginManager()
    snapshot = SimpleNamespace(value=object())
    manager.scene_runtime = SimpleNamespace(  # type: ignore[assignment]
        get_scene_candidates=lambda: snapshot.value
    )
    monkeypatch.setattr(
        decision_plugin,
        "get_config",
        lambda: SimpleNamespace(plugin_enable=True, scene_persist_enabled=True),
    )

    assert manager.runtime_state.status is DecisionRuntimeStatus.READY

    snapshot.value = None

    assert manager.runtime_state.status is DecisionRuntimeStatus.FAILED


def _patch_memory_plugin_manager(
    monkeypatch: Any,
    memory_manager: object,
) -> None:
    """替换 komari_memory 模块的插件管理器出口（惰性解析，逐调用生效）。"""
    memory_module = sys.modules["komari_bot.plugins.komari_memory"]
    monkeypatch.setattr(
        memory_module,
        "get_plugin_manager",
        lambda: memory_manager,
        raising=False,
    )


def _patch_decision_manager(
    monkeypatch: Any,
    manager: object,
) -> None:
    monkeypatch.setattr(decision_plugin, "get_plugin_manager", lambda: manager)


def test_get_decision_engine_returns_none_when_memory_manager_missing(
    monkeypatch: Any,
) -> None:
    _patch_memory_plugin_manager(monkeypatch, None)
    _patch_decision_manager(monkeypatch, None)

    assert decision_plugin.get_decision_engine() is None


def test_get_decision_engine_returns_none_when_redis_not_ready(
    monkeypatch: Any,
) -> None:
    _patch_memory_plugin_manager(monkeypatch, SimpleNamespace(redis=None))
    _patch_decision_manager(monkeypatch, None)

    assert decision_plugin.get_decision_engine() is None


def test_get_decision_engine_builds_and_caches_engine(
    monkeypatch: Any,
) -> None:
    redis = object()
    _patch_memory_plugin_manager(monkeypatch, SimpleNamespace(redis=redis))
    _patch_decision_manager(monkeypatch, None)

    engine = decision_plugin.get_decision_engine()
    assert isinstance(engine, DecisionEngine)
    assert decision_plugin.get_decision_engine() is engine


def test_get_decision_engine_rebuilds_on_redis_identity_change(
    monkeypatch: Any,
) -> None:
    holder = SimpleNamespace(redis=object())
    _patch_memory_plugin_manager(monkeypatch, holder)
    _patch_decision_manager(monkeypatch, None)

    first = decision_plugin.get_decision_engine()
    holder.redis = object()
    second = decision_plugin.get_decision_engine()

    assert isinstance(first, DecisionEngine)
    assert isinstance(second, DecisionEngine)
    assert second is not first


def test_get_decision_engine_rebuilds_on_scene_runtime_identity_change(
    monkeypatch: Any,
) -> None:
    _patch_memory_plugin_manager(monkeypatch, SimpleNamespace(redis=object()))
    manager = decision_plugin.PluginManager()
    manager.scene_runtime = cast("Any", object())
    _patch_decision_manager(monkeypatch, manager)

    first = decision_plugin.get_decision_engine()
    manager.scene_runtime = cast("Any", object())
    second = decision_plugin.get_decision_engine()

    assert isinstance(first, DecisionEngine)
    assert isinstance(second, DecisionEngine)
    assert second is not first


def test_get_decision_engine_keeps_cache_across_redis_gap(
    monkeypatch: Any,
) -> None:
    """Redis 短暂未就绪不清缓存，同身份恢复后仍返回原引擎（与聊天懒构建等价）。"""
    holder = SimpleNamespace(redis=object())
    _patch_memory_plugin_manager(monkeypatch, holder)
    _patch_decision_manager(monkeypatch, None)

    engine = decision_plugin.get_decision_engine()
    assert isinstance(engine, DecisionEngine)

    holder.redis = None
    assert decision_plugin.get_decision_engine() is None

    holder.redis = engine_redis = object()
    rebuilt = decision_plugin.get_decision_engine()
    assert isinstance(rebuilt, DecisionEngine)
    assert rebuilt is not engine

    holder.redis = None
    assert decision_plugin.get_decision_engine() is None

    holder.redis = engine_redis
    assert decision_plugin.get_decision_engine() is rebuilt
