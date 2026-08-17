"""SceneAdminService 公开运维接口测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from komari_bot.plugins.komari_decision.services.scene_admin_service import (
    SceneAdminService,
)

_REQUIRED_FIXED_SCENE_KEYS = (
    "NOISE",
    "MEANINGFUL",
    "CALL_DIRECT",
    "CALL_MENTION",
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


def _build_service(repository: FakeSceneRepository) -> SceneAdminService:
    return SceneAdminService(repository=cast("Any", repository))


def test_prune_old_sets_keeps_latest_and_active(monkeypatch: Any) -> None:
    repository = FakeSceneRepository()
    repository.active_set_id = 1
    service = _build_service(repository)
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
    service = _build_service(repository)
    monkeypatch.setattr(
        "komari_bot.plugins.komari_decision.services.scene_admin_service.get_config",
        lambda: SimpleNamespace(scene_keep_versions=2),
    )

    result = asyncio.run(service.prune_old_sets())
    assert result.kept_set_ids == [5, 4, 2]
    assert result.deleted_set_ids == [3, 1]
    assert repository.deleted_ids == [3, 1]


@pytest.mark.parametrize("scene_key", _REQUIRED_FIXED_SCENE_KEYS)
def test_upsert_scene_rejects_required_fixed_type_change_before_repository(
    scene_key: str,
) -> None:
    repository = FakeSceneRepository()
    service = _build_service(repository)

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


@pytest.mark.parametrize("scene_key", _REQUIRED_FIXED_SCENE_KEYS)
def test_upsert_scene_rejects_disabled_required_fixed_before_repository(
    scene_key: str,
) -> None:
    repository = FakeSceneRepository()
    service = _build_service(repository)

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
    service = _build_service(repository)

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
