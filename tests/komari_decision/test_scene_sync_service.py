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
        self.existing_scene_set: dict | None = None
        self.reusable_by_key: dict[str, dict] = {}
        self.created_payload: dict | None = None
        self.inserted_items: list[dict] = []
        self.create_called = 0
        self.refresh_progress_called = 0

    async def get_or_create_scene_set(
        self,
        *,
        source_path: str,
        source_hash: str,
        embedding_model: str,
        embedding_instruction_hash: str,
        status: str = "BUILDING",
    ) -> tuple[dict, bool]:
        self.created_payload = {
            "source_path": source_path,
            "source_hash": source_hash,
            "embedding_model": embedding_model,
            "embedding_instruction_hash": embedding_instruction_hash,
            "status": status,
        }
        if self.existing_scene_set is not None:
            return dict(self.existing_scene_set), False
        self.create_called += 1
        self.existing_scene_set = {
            "id": 101,
            "status": "BUILDING",
            "item_total": 0,
            "item_ready": 0,
            "item_failed": 0,
        }
        return dict(self.existing_scene_set), True

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
        existing_keys = {str(item["scene_key"]) for item in self.inserted_items}
        new_items = [
            dict(item)
            for item in items
            if str(item["scene_key"]) not in existing_keys
        ]
        self.inserted_items.extend(new_items)
        return len(new_items)

    async def refresh_set_progress(self, set_id: int) -> dict:
        assert set_id == 101
        self.refresh_progress_called += 1
        ready = len(
            [item for item in self.inserted_items if item["status"] == "READY"]
        )
        failed = len(
            [item for item in self.inserted_items if item["status"] == "FAILED"]
        )
        total = len(self.inserted_items)
        status = "READY" if total > 0 and ready == total else "BUILDING"
        return {
            "id": 101,
            "status": status,
            "item_total": total,
            "item_ready": ready,
            "item_failed": failed,
            "previous_status": "BUILDING",
        }


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
    repository.existing_scene_set = {
        "id": 7,
        "source_hash": payload.source_hash,
        "embedding_model": "model-x",
        "embedding_instruction_hash": instruction_hash,
        "status": "READY",
        "item_total": 5,
        "item_ready": 5,
        "item_failed": 0,
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
    assert repository.refresh_progress_called == 1

    assert len(repository.inserted_items) == 2
    assert repository.inserted_items[0]["scene_key"] == "NOISE"
    assert repository.inserted_items[0]["status"] == "READY"
    assert repository.inserted_items[1]["scene_key"] == "SCENE_HELLO"
    assert repository.inserted_items[1]["status"] == "PENDING"


def test_build_scene_set_recovers_existing_empty_build(monkeypatch: Any) -> None:
    _patch_config(monkeypatch)
    payload = _make_template_payload()
    repository = FakeSceneRepository()
    repository.existing_scene_set = {
        "id": 101,
        "status": "BUILDING",
        "item_total": 0,
        "item_ready": 0,
        "item_failed": 0,
    }
    service = SceneSyncService(
        repository=cast("Any", repository),
        loader=cast("Any", FakeLoader(payload)),
    )
    monkeypatch.setattr(service, "_resolve_embedding_model", lambda: "model-x")

    result = asyncio.run(service.build_scene_set())

    assert result.set_id == 101
    assert result.created is False
    assert result.reused_existing_set is True
    assert result.inserted_count == 2
    assert len(repository.inserted_items) == 2
