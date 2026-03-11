"""SceneSyncTaskManager 测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from komari_bot.plugins.komari_decision.handlers.scene_sync_worker import (
    SceneSyncTaskManager,
)


class FakeRepository:
    def __init__(self, *, active_set_id: int | None = None) -> None:
        self.active_set_id = active_set_id

    async def get_scene_set(self, set_id: int) -> dict[str, Any] | None:
        return {"id": set_id, "status": "READY"}

    async def get_active_set(self) -> dict[str, Any] | None:
        if self.active_set_id is None:
            return None
        return {"id": self.active_set_id}


class FakeSyncService:
    def __init__(self, result: SimpleNamespace) -> None:
        self._result = result

    async def build_scene_set(self) -> SimpleNamespace:
        return self._result


class FakeEmbeddingWorker:
    def __init__(self) -> None:
        self.calls: list[int] = []

    async def embed_pending_batch(self, set_id: int) -> SimpleNamespace:
        self.calls.append(set_id)
        return SimpleNamespace(pending_count=0, fetched_count=0)


class FakeRuntimeService:
    def __init__(self) -> None:
        self.switched_ids: list[int] = []
        self.refresh_calls = 0

    async def switch_active_set(self, set_id: int) -> None:
        self.switched_ids.append(set_id)

    async def refresh_if_runtime_updated(self) -> bool:
        self.refresh_calls += 1
        return True


class FakeAdminService:
    def __init__(self) -> None:
        self.prune_calls = 0

    async def prune_old_sets(self) -> SimpleNamespace:
        self.prune_calls += 1
        return SimpleNamespace(deleted_set_ids=[1], kept_set_ids=[3, 2])


def _patch_config(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "komari_bot.plugins.komari_decision.handlers.scene_sync_worker.get_config",
        lambda: SimpleNamespace(
            scene_persist_enabled=True,
            scene_sync_poll_seconds=30,
        ),
    )


def test_execute_task_prunes_after_new_ready_set(monkeypatch: Any) -> None:
    manager = SceneSyncTaskManager()
    repository = FakeRepository(active_set_id=2)
    runtime_service = FakeRuntimeService()
    admin_service = FakeAdminService()
    _patch_config(monkeypatch)
    manager.register(
        cast("Any", repository),
        cast("Any", admin_service),
        cast(
            "Any",
            FakeSyncService(
                SimpleNamespace(
                    set_id=3,
                    created=True,
                    reused_existing_set=False,
                    pending_count=0,
                )
            ),
        ),
        cast("Any", FakeEmbeddingWorker()),
        cast("Any", runtime_service),
    )

    asyncio.run(manager._execute_task())
    assert runtime_service.switched_ids == [3]
    assert runtime_service.refresh_calls == 1
    assert admin_service.prune_calls == 1


def test_execute_task_skips_prune_when_set_not_new_and_not_activated(
    monkeypatch: Any,
) -> None:
    manager = SceneSyncTaskManager()
    repository = FakeRepository(active_set_id=3)
    runtime_service = FakeRuntimeService()
    admin_service = FakeAdminService()
    _patch_config(monkeypatch)
    manager.register(
        cast("Any", repository),
        cast("Any", admin_service),
        cast(
            "Any",
            FakeSyncService(
                SimpleNamespace(
                    set_id=3,
                    created=False,
                    reused_existing_set=True,
                    pending_count=0,
                )
            ),
        ),
        cast("Any", FakeEmbeddingWorker()),
        cast("Any", runtime_service),
    )

    asyncio.run(manager._execute_task())
    assert runtime_service.switched_ids == []
    assert runtime_service.refresh_calls == 1
    assert admin_service.prune_calls == 0
