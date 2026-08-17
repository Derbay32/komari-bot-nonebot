"""SceneRuntimeService 切换与缓存测试。"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from komari_bot.plugins.komari_decision.services.scene_runtime_service import (
    SceneRuntimeService,
)
from tests.komari_decision.required_fixed_scene_keys import REQUIRED_FIXED_SCENE_KEYS

_ITEM_LABELS = {
    "NOISE": "noise",
    "MEANINGFUL": "meaningful",
    "CALL_DIRECT": "direct",
    "CALL_MENTION": "mention",
}


def _build_items(tag: str) -> list[dict]:
    items = [
        {
            "scene_key": scene_key,
            "scene_type": "fixed",
            "content_text": f"{tag} {_ITEM_LABELS[scene_key]}",
            "embedding": [0.1 * (index + 1), 0.1 * (index + 1)],
            "order_index": index,
            "status": "READY",
            "enabled": True,
        }
        for index, scene_key in enumerate(REQUIRED_FIXED_SCENE_KEYS)
    ]
    items.append(
        {
            "scene_key": f"SCENE_{tag.upper()}",
            "scene_type": "general",
            "content_text": f"{tag} scene",
            "embedding": [0.5, 0.5],
            "order_index": 4,
            "status": "READY",
            "enabled": True,
        }
    )
    return items


class FakeSceneRepository:
    def __init__(self) -> None:
        self.active_id = 1
        self.runtime_counter = 1
        self.sets = {
            1: {"id": 1, "status": "READY"},
            2: {"id": 2, "status": "READY"},
        }
        self.items = {1: _build_items("v1"), 2: _build_items("v2")}

    async def get_active_set(self) -> dict | None:
        current = self.sets.get(self.active_id)
        if current is None:
            return None
        return {
            "id": self.active_id,
            "status": current["status"],
            "runtime_updated_at": f"ts-{self.runtime_counter}",
        }

    async def list_items_by_set(
        self,
        set_id: int,
        status: str | None = None,
        *,
        enabled_only: bool = False,
        limit: int | None = None,
    ) -> list[dict]:
        del status, enabled_only, limit
        return [dict(item) for item in self.items[set_id]]

    async def switch_active_set(self, set_id: int) -> None:
        scene_set = self.sets.get(set_id)
        if scene_set is None or scene_set.get("status") != "READY":
            msg = "set not ready"
            raise ValueError(msg)
        self.active_id = set_id
        self.runtime_counter += 1


def test_runtime_load_and_switch() -> None:
    repository = FakeSceneRepository()
    service = SceneRuntimeService(cast("Any", repository))

    loaded = asyncio.run(service.load_active_set_cache())
    assert loaded is True
    snapshot = service.get_scene_candidates()
    assert snapshot is not None
    assert snapshot.set_id == 1
    assert snapshot.fixed_candidates["NOISE"] == "v1 noise"

    changed = asyncio.run(service.refresh_if_runtime_updated())
    assert changed is False

    switched_snapshot = asyncio.run(service.switch_active_set(2))
    assert switched_snapshot.set_id == 2
    assert switched_snapshot.fixed_candidates["NOISE"] == "v2 noise"

    changed_after = asyncio.run(service.refresh_if_runtime_updated())
    assert changed_after is False


@pytest.mark.parametrize(
    "scene_key",
    REQUIRED_FIXED_SCENE_KEYS,
)
def test_runtime_rejects_missing_required_fixed_candidate(scene_key: str) -> None:
    repository = FakeSceneRepository()
    repository.items[1] = [
        item for item in repository.items[1] if item["scene_key"] != scene_key
    ]
    service = SceneRuntimeService(cast("Any", repository))

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(service.load_active_set_cache())

    assert scene_key in str(exc_info.value)
    assert "固定候选或 embedding" in str(exc_info.value)


@pytest.mark.parametrize(
    "scene_key",
    REQUIRED_FIXED_SCENE_KEYS,
)
def test_runtime_rejects_missing_required_fixed_embedding(scene_key: str) -> None:
    repository = FakeSceneRepository()
    item = next(
        item for item in repository.items[1] if item["scene_key"] == scene_key
    )
    item["embedding"] = None
    service = SceneRuntimeService(cast("Any", repository))

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(service.load_active_set_cache())

    assert scene_key in str(exc_info.value)
    assert "固定候选或 embedding" in str(exc_info.value)
