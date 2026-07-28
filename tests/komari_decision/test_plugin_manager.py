"""KomariDecision PluginManager tests."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from typing import Any

import nonebot.plugin

import komari_bot.plugins.komari_decision as decision_plugin


class _FakeSceneRepository:
    def __init__(self, pg_pool: object) -> None:
        self.pg_pool = pg_pool

    async def ensure_schema(self) -> None:
        return None


class _FakeSceneRuntimeService:
    def __init__(self, repository: _FakeSceneRepository) -> None:
        self.repository = repository

    async def load_active_set_cache(self) -> bool:
        return True


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
        "get_config",
        lambda: SimpleNamespace(scene_persist_enabled=True),
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
