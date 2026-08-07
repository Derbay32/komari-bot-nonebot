"""SceneAdminService 单元测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

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
        self.scene_rows: list[dict[str, Any]] = [
            {"scene_key": "NOISE", "enabled": True},
            {"scene_key": "MEANINGFUL", "enabled": False},
        ]
        self.list_scenes_calls: list[bool] = []
        self.get_scene_by_key_calls: list[str] = []
        self.upsert_scene_calls: list[dict[str, Any]] = []
        self.list_scenes_error: Exception | None = None
        self.upsert_scene_error: Exception | None = None

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

    async def list_scenes(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        if self.list_scenes_error is not None:
            raise self.list_scenes_error
        self.list_scenes_calls.append(enabled_only)
        return [dict(row) for row in self.scene_rows]

    async def get_scene_by_key(self, scene_key: str) -> dict[str, Any] | None:
        self.get_scene_by_key_calls.append(scene_key)
        for row in self.scene_rows:
            if row["scene_key"] == scene_key:
                return dict(row)
        return None

    async def upsert_scene(
        self,
        *,
        scene_key: str,
        scene_type: str,
        content_text: str,
        enabled: bool = True,
        order_index: int = 0,
    ) -> dict[str, Any]:
        if self.upsert_scene_error is not None:
            raise self.upsert_scene_error
        call = {
            "scene_key": scene_key,
            "scene_type": scene_type,
            "content_text": content_text,
            "enabled": enabled,
            "order_index": order_index,
        }
        self.upsert_scene_calls.append(call)
        return dict(call)


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


def _build_service(repository: FakeSceneRepository) -> SceneAdminService:
    return SceneAdminService(
        repository=cast("Any", repository),
        runtime_service=cast("Any", FakeSceneRuntimeService()),
        embedding_worker=cast("Any", FakeSceneEmbeddingWorker([])),
    )


def test_list_scenes_delegates_with_enabled_only_flag() -> None:
    repository = FakeSceneRepository()
    service = _build_service(repository)

    rows = asyncio.run(service.list_scenes(enabled_only=True))
    assert rows == repository.scene_rows
    assert repository.list_scenes_calls == [True]

    rows = asyncio.run(service.list_scenes())
    assert rows == repository.scene_rows
    assert repository.list_scenes_calls == [True, False]


def test_get_scene_by_key_delegates_and_returns_none_when_missing() -> None:
    repository = FakeSceneRepository()
    service = _build_service(repository)

    row = asyncio.run(service.get_scene_by_key("NOISE"))
    assert row == {"scene_key": "NOISE", "enabled": True}

    missing = asyncio.run(service.get_scene_by_key("NOT_EXIST"))
    assert missing is None
    assert repository.get_scene_by_key_calls == ["NOISE", "NOT_EXIST"]


def test_upsert_scene_delegates_keyword_arguments() -> None:
    repository = FakeSceneRepository()
    service = _build_service(repository)

    row = asyncio.run(
        service.upsert_scene(
            scene_key="GREETING",
            scene_type="general",
            content_text="打招呼",
            enabled=False,
            order_index=7,
        )
    )
    assert row == {
        "scene_key": "GREETING",
        "scene_type": "general",
        "content_text": "打招呼",
        "enabled": False,
        "order_index": 7,
    }
    assert repository.upsert_scene_calls == [row]


def test_passthrough_methods_propagate_repository_errors() -> None:
    repository = FakeSceneRepository()
    repository.list_scenes_error = RuntimeError("list 失败")
    repository.upsert_scene_error = ValueError("scene_key 不能为空")
    service = _build_service(repository)

    with pytest.raises(RuntimeError, match="list 失败"):
        asyncio.run(service.list_scenes())
    with pytest.raises(ValueError, match="scene_key 不能为空"):
        asyncio.run(
            service.upsert_scene(
                scene_key="",
                scene_type="general",
                content_text="x",
            )
        )
