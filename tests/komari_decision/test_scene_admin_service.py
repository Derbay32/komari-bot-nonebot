"""SceneAdminService 单元测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from komari_bot.plugins.komari_decision.services.scene_admin_service import (
    SceneAdminService,
)


class FakeSceneRepository:
    def __init__(self) -> None:
        self.active_set_id = 3
        self.ready_sets = [
            {"id": 3},
            {"id": 2},
            {"id": 1},
        ]
        self.deleted_ids: list[int] = []
        self.reopened_failed_sets: list[int] = []

    async def list_ready_sets(self, *, limit: int | None = None) -> list[dict[str, int]]:
        if limit is None:
            return [dict(item) for item in self.ready_sets]
        return [dict(item) for item in self.ready_sets[:limit]]

    async def get_active_set(self) -> dict[str, int] | None:
        if self.active_set_id is None:
            return None
        return {"id": self.active_set_id}

    async def delete_set(self, set_id: int) -> bool:
        self.deleted_ids.append(set_id)
        return True

    async def reopen_failed_set(self, set_id: int) -> int:
        self.reopened_failed_sets.append(set_id)
        return 2


class FakeSceneRuntimeService:
    def __init__(self) -> None:
        self.switched_ids: list[int] = []

    async def switch_active_set(self, set_id: int) -> SimpleNamespace:
        self.switched_ids.append(set_id)
        return SimpleNamespace(set_id=set_id)


class FakeSceneEmbeddingWorker:
    def __init__(self, batches: list[SimpleNamespace]) -> None:
        self._batches = list(batches)
        self.called_with: list[int] = []

    async def embed_pending_batch(self, set_id: int) -> SimpleNamespace:
        self.called_with.append(set_id)
        if self._batches:
            return self._batches.pop(0)
        return SimpleNamespace(
            pending_count=0,
            fetched_count=0,
            set_status="READY",
            transitioned_ready=False,
            transitioned_failed=False,
        )

    async def refresh_set_counters(self, set_id: int) -> SimpleNamespace:
        del set_id
        return SimpleNamespace(
            pending=0,
            status="READY",
            transitioned_ready=False,
            transitioned_failed=False,
        )


def test_activate_ready_set_switches_runtime() -> None:
    service = SceneAdminService(
        repository=cast("Any", FakeSceneRepository()),
        runtime_service=cast("Any", FakeSceneRuntimeService()),
        embedding_worker=cast("Any", FakeSceneEmbeddingWorker([])),
    )

    snapshot = asyncio.run(service.activate_ready_set(9))
    assert snapshot.set_id == 9


def test_rollback_to_previous_ready_uses_next_older_set() -> None:
    repository = FakeSceneRepository()
    runtime_service = FakeSceneRuntimeService()
    service = SceneAdminService(
        repository=cast("Any", repository),
        runtime_service=cast("Any", runtime_service),
        embedding_worker=cast("Any", FakeSceneEmbeddingWorker([])),
    )

    snapshot = asyncio.run(service.rollback_to_previous_ready())
    assert snapshot.set_id == 2
    assert runtime_service.switched_ids == [2]


def test_retry_failed_set_drains_batches_until_pending_cleared() -> None:
    repository = FakeSceneRepository()
    worker = FakeSceneEmbeddingWorker(
        [
            SimpleNamespace(
                pending_count=1,
                fetched_count=1,
                set_status="BUILDING",
                transitioned_ready=False,
                transitioned_failed=False,
            ),
            SimpleNamespace(
                pending_count=0,
                fetched_count=1,
                set_status="READY",
                transitioned_ready=True,
                transitioned_failed=False,
            ),
        ]
    )
    service = SceneAdminService(
        repository=cast("Any", repository),
        runtime_service=cast("Any", FakeSceneRuntimeService()),
        embedding_worker=cast("Any", worker),
    )

    result = asyncio.run(service.retry_failed_set(12))
    assert result.set_id == 12
    assert result.reset_failed_items == 2
    assert result.pending_count == 0
    assert result.status == "READY"
    assert result.transitioned_ready is True
    assert repository.reopened_failed_sets == [12]
    assert worker.called_with == [12, 12]


def test_prune_old_sets_keeps_latest_and_active(monkeypatch: Any) -> None:
    repository = FakeSceneRepository()
    repository.active_set_id = 1
    service = SceneAdminService(
        repository=cast("Any", repository),
        runtime_service=cast("Any", FakeSceneRuntimeService()),
        embedding_worker=cast("Any", FakeSceneEmbeddingWorker([])),
    )
    monkeypatch.setattr(
        "komari_bot.plugins.komari_decision.services.scene_admin_service.get_config",
        lambda: SimpleNamespace(scene_keep_versions=2),
    )

    result = asyncio.run(service.prune_old_sets())
    assert result.kept_set_ids == [3, 2, 1]
    assert result.deleted_set_ids == []
    assert result.active_set_id == 1


def test_prune_old_sets_deletes_ready_sets_outside_keep_window(
    monkeypatch: Any,
) -> None:
    repository = FakeSceneRepository()
    repository.ready_sets = [
        {"id": 5},
        {"id": 4},
        {"id": 3},
        {"id": 2},
        {"id": 1},
    ]
    repository.active_set_id = 2
    service = SceneAdminService(
        repository=cast("Any", repository),
        runtime_service=cast("Any", FakeSceneRuntimeService()),
        embedding_worker=cast("Any", FakeSceneEmbeddingWorker([])),
    )
    monkeypatch.setattr(
        "komari_bot.plugins.komari_decision.services.scene_admin_service.get_config",
        lambda: SimpleNamespace(scene_keep_versions=2),
    )

    result = asyncio.run(service.prune_old_sets())
    assert result.kept_set_ids == [5, 4, 2]
    assert result.deleted_set_ids == [3, 1]
    assert repository.deleted_ids == [3, 1]
