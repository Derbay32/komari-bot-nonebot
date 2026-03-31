"""SceneSyncService 单元测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from komari_bot.plugins.komari_decision.services import scene_sync_service as sss
from komari_bot.plugins.komari_decision.services.scene_sync_service import (
    SceneSyncService,
)
from komari_bot.plugins.komari_decision.services.scene_template_loader import (
    SceneTemplateItem,
    SceneTemplatePayload,
)


class FakeSceneRepository:
    def __init__(self) -> None:
        self.latest_ready: dict | None = None
        self.latest_building: dict | None = None
        self.reusable_by_key: dict[str, dict] = {}
        self.created_payload: dict | None = None
        self.inserted_items: list[dict] = []
        self.create_called = 0
        self.mark_ready_called = 0
        self.update_counter_called = 0

    async def get_latest_ready_set(self) -> dict | None:
        return self.latest_ready

    async def get_latest_set_by_fingerprint(
        self,
        source_hash: str,
        embedding_model: str,
        embedding_instruction_hash: str,
        *,
        status: str | None = None,
    ) -> dict | None:
        del source_hash, embedding_model, embedding_instruction_hash
        if status == "BUILDING":
            return self.latest_building
        return None

    async def create_scene_set(
        self,
        source_path: str,
        source_hash: str,
        embedding_model: str,
        embedding_instruction_hash: str,
        status: str = "BUILDING",
    ) -> int:
        self.create_called += 1
        self.created_payload = {
            "source_path": source_path,
            "source_hash": source_hash,
            "embedding_model": embedding_model,
            "embedding_instruction_hash": embedding_instruction_hash,
            "status": status,
        }
        return 101

    async def find_reusable_ready_item(
        self,
        scene_key: str,
        content_hash: str,
        embedding_model: str,
        embedding_instruction_hash: str,
    ) -> dict | None:
        del content_hash, embedding_model, embedding_instruction_hash
        return self.reusable_by_key.get(scene_key)

    async def insert_scene_items(self, set_id: int, items: list[dict]) -> int:
        assert set_id == 101
        self.inserted_items = list(items)
        return len(items)

    async def update_set_counters(self, set_id: int) -> None:
        assert set_id == 101
        self.update_counter_called += 1

    async def mark_set_ready(self, set_id: int) -> None:
        assert set_id == 101
        self.mark_ready_called += 1


class FakeLoader:
    def __init__(self, payload: SceneTemplatePayload) -> None:
        self.payload = payload

    def load_scene_template(self) -> SceneTemplatePayload:
        return self.payload


def _make_template_payload() -> SceneTemplatePayload:
    items = [
        SceneTemplateItem(
            scene_key="NOISE",
            scene_type="fixed",
            content_text="noise",
            enabled=True,
            order_index=0,
            content_hash="h-noise",
        ),
        SceneTemplateItem(
            scene_key="SCENE_HELLO",
            scene_type="general",
            content_text="hello scene",
            enabled=True,
            order_index=1,
            content_hash="h-scene",
        ),
    ]
    return SceneTemplatePayload(
        source_path="/tmp/scene.yaml",
        source_hash="source-hash-1",
        fixed_candidates={"NOISE": "noise"},
        general_scenes=[{"id": "SCENE_HELLO", "text": "hello scene"}],
        items=items,
    )


def _patch_config(monkeypatch: Any) -> None:
    config = SimpleNamespace(embedding_instruction_scene="scene instruction")
    monkeypatch.setattr(sss, "get_config", lambda: config)


def test_build_scene_set_reuse_latest_ready(monkeypatch: Any) -> None:
    _patch_config(monkeypatch)
    payload = _make_template_payload()
    repository = FakeSceneRepository()
    service = SceneSyncService(
        repository=cast("Any", repository),
        loader=cast("Any", FakeLoader(payload)),
    )
    monkeypatch.setattr(service, "_resolve_embedding_model", lambda: "model-x")

    instruction_hash = service._instruction_hash("scene instruction")
    repository.latest_ready = {
        "id": 7,
        "source_hash": payload.source_hash,
        "embedding_model": "model-x",
        "embedding_instruction_hash": instruction_hash,
        "item_ready": 5,
    }

    result = asyncio.run(service.build_scene_set())
    assert result.set_id == 7
    assert result.created is False
    assert result.reused_existing_set is True
    assert result.ready_count == 5
    assert repository.create_called == 0


def test_build_scene_set_create_and_partial_reuse(
    monkeypatch: Any,
) -> None:
    _patch_config(monkeypatch)
    payload = _make_template_payload()
    repository = FakeSceneRepository()
    repository.reusable_by_key["NOISE"] = {
        "embedding": [0.1, 0.2],
        "embedding_dim": 2,
        "embedded_at": "2026-03-05T00:00:00+08:00",
    }
    service = SceneSyncService(
        repository=cast("Any", repository),
        loader=cast("Any", FakeLoader(payload)),
    )
    monkeypatch.setattr(service, "_resolve_embedding_model", lambda: "model-x")

    result = asyncio.run(service.build_scene_set())
    assert result.set_id == 101
    assert result.created is True
    assert result.reused_existing_set is False
    assert result.inserted_count == 2
    assert result.ready_count == 1
    assert result.pending_count == 1
    assert repository.create_called == 1
    assert repository.update_counter_called == 1
    assert repository.mark_ready_called == 0

    assert len(repository.inserted_items) == 2
    assert repository.inserted_items[0]["scene_key"] == "NOISE"
    assert repository.inserted_items[0]["status"] == "READY"
    assert repository.inserted_items[1]["scene_key"] == "SCENE_HELLO"
    assert repository.inserted_items[1]["status"] == "PENDING"
