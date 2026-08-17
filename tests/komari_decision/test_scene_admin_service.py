"""SceneAdminService 公开运维接口测试。"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from typing import Any, cast

import pytest

from komari_bot.plugins.komari_decision.services.scene_admin_service import (
    SceneAdminService,
)
from komari_bot.plugins.komari_decision.services.scene_sync_service import (
    SceneSyncResult,
)
from tests.komari_decision.required_fixed_scene_keys import REQUIRED_FIXED_SCENE_KEYS

_UNUSED_SYNC_SERVICE = SimpleNamespace()


class FakeSceneRepository:
    def __init__(self) -> None:
        self.active_set_id = 3
        self.ready_sets = [
            {"id": 3},
            {"id": 2},
            {"id": 1},
        ]
        self.deleted_ids: list[int] = []
        self.upsert_scene_calls: list[dict[str, Any]] = []

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

    async def upsert_scene(
        self,
        *,
        scene_key: str,
        scene_type: str,
        content_text: str,
        enabled: bool = True,
        order_index: int = 0,
    ) -> dict[str, Any]:
        call = {
            "scene_key": scene_key,
            "scene_type": scene_type,
            "content_text": content_text,
            "enabled": enabled,
            "order_index": order_index,
        }
        self.upsert_scene_calls.append(call)
        return dict(call)


class _FakeSyncService:
    """SceneSyncService 桩：返回预设的 SceneSyncResult。"""

    def __init__(self, result: SceneSyncResult) -> None:
        self._result = result
        self.build_calls = 0

    async def build_scene_set(self) -> SceneSyncResult:
        self.build_calls += 1
        return self._result


def _build_service(
    repository: FakeSceneRepository,
    sync_service: object,
) -> SceneAdminService:
    """构造 SceneAdminService（正式双参签名 (repository, sync_service)）。"""
    admin_class = cast("Any", SceneAdminService)
    return admin_class(
        repository=cast("Any", repository),
        sync_service=sync_service,
    )


def test_prune_old_sets_keeps_latest_and_active(monkeypatch: Any) -> None:
    repository = FakeSceneRepository()
    repository.active_set_id = 1
    service = _build_service(repository, _UNUSED_SYNC_SERVICE)
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
    service = _build_service(repository, _UNUSED_SYNC_SERVICE)
    monkeypatch.setattr(
        "komari_bot.plugins.komari_decision.services.scene_admin_service.get_config",
        lambda: SimpleNamespace(scene_keep_versions=2),
    )

    result = asyncio.run(service.prune_old_sets())
    assert result.kept_set_ids == [5, 4, 2]
    assert result.deleted_set_ids == [3, 1]
    assert repository.deleted_ids == [3, 1]


@pytest.mark.parametrize("scene_key", REQUIRED_FIXED_SCENE_KEYS)
def test_upsert_scene_rejects_required_fixed_type_change_before_repository(
    scene_key: str,
) -> None:
    repository = FakeSceneRepository()
    service = _build_service(repository, _UNUSED_SYNC_SERVICE)

    with pytest.raises(ValueError, match="必需 fixed scene 不允许改为其他类型"):
        asyncio.run(
            service.upsert_scene(
                scene_key=scene_key,
                scene_type="general",
                content_text="非法改型",
                enabled=True,
            )
        )

    assert repository.upsert_scene_calls == []


@pytest.mark.parametrize("scene_key", REQUIRED_FIXED_SCENE_KEYS)
def test_upsert_scene_rejects_disabled_required_fixed_before_repository(
    scene_key: str,
) -> None:
    repository = FakeSceneRepository()
    service = _build_service(repository, _UNUSED_SYNC_SERVICE)

    with pytest.raises(ValueError, match="必需 fixed scene 不允许禁用"):
        asyncio.run(
            service.upsert_scene(
                scene_key=scene_key,
                scene_type="fixed",
                content_text="非法禁用",
                enabled=False,
            )
        )

    assert repository.upsert_scene_calls == []


@pytest.mark.parametrize(
    "write",
    [
        {"scene_key": "NOISE", "scene_type": "fixed", "enabled": True},
        {"scene_key": "GREETING", "scene_type": "general", "enabled": False},
    ],
)
def test_upsert_scene_persists_legal_fixed_and_general_writes(
    write: dict[str, Any],
) -> None:
    repository = FakeSceneRepository()
    service = _build_service(repository, _UNUSED_SYNC_SERVICE)

    row = asyncio.run(
        service.upsert_scene(
            scene_key=write["scene_key"],
            scene_type=write["scene_type"],
            content_text="合法内容",
            enabled=write["enabled"],
            order_index=7,
        )
    )

    expected = {
        **write,
        "content_text": "合法内容",
        "order_index": 7,
    }
    assert row == expected
    assert repository.upsert_scene_calls == [expected]


def test_upsert_scene_rejects_padded_required_fixed_key_before_repository() -> None:
    """回归：场景运维写入路径先规范化 key/type，再执行 required-fixed 裁决。

    本测试用 scene_key=" NOISE "、scene_type="general" 区分规范化后裁决
    与原值裁决：若按原值裁决，padded key 不会命中 required-fixed 集合，
    会漏判并触达 Repository；规范化后裁决则应在 Repository 调用前抛出
    领域 ValueError。
    """
    repository = FakeSceneRepository()
    service = _build_service(repository, _UNUSED_SYNC_SERVICE)

    with pytest.raises(ValueError, match="必需 fixed scene 不允许改为其他类型"):
        asyncio.run(
            service.upsert_scene(
                scene_key=" NOISE ",
                scene_type="general",
                content_text="非法改型",
                enabled=True,
            )
        )

    assert repository.upsert_scene_calls == []


def test_sync_scenes_returns_same_scene_sync_result_object() -> None:
    """TSK-179: sync_scenes() 原样返回同一 SceneSyncResult 对象，不新增 DTO。"""
    assert list(inspect.signature(SceneAdminService).parameters) == [
        "repository",
        "sync_service",
    ], "TSK-179 要求 SceneAdminService 双参构造 (repository, sync_service)"

    repository = FakeSceneRepository()
    sync_result = SceneSyncResult(
        set_id=5,
        created=True,
        reused_existing_set=False,
        inserted_count=2,
        ready_count=1,
        pending_count=1,
    )
    sync_service = _FakeSyncService(sync_result)
    admin_class = cast("Any", SceneAdminService)
    service = admin_class(
        repository=cast("Any", repository),
        sync_service=sync_service,
    )

    result = asyncio.run(service.sync_scenes())

    assert result is sync_result
    assert sync_service.build_calls == 1
