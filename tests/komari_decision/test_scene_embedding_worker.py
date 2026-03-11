"""SceneEmbeddingWorker 单元测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from komari_bot.plugins.komari_decision.services import scene_embedding_worker as sew
from komari_bot.plugins.komari_decision.services.scene_embedding_worker import (
    SceneEmbeddingWorker,
)


class FakeSceneRepository:
    def __init__(self) -> None:
        self.items = {
            1: {"id": 1, "content_text": "a", "status": "PENDING"},
            2: {"id": 2, "content_text": "b", "status": "PENDING"},
        }
        self.scene_set = {
            "id": 10,
            "status": "BUILDING",
            "item_total": 2,
            "item_ready": 0,
            "item_failed": 0,
        }

    async def fetch_pending_items(self, set_id: int, *, limit: int = 32) -> list[dict]:
        assert set_id == 10
        pending = [item for item in self.items.values() if item["status"] == "PENDING"]
        return [dict(item) for item in pending[:limit]]

    async def mark_item_ready(
        self, item_id: int, embedding: list[float], embedding_dim: int
    ) -> None:
        item = self.items[item_id]
        item["status"] = "READY"
        item["embedding"] = embedding
        item["embedding_dim"] = embedding_dim

    async def mark_item_failed(self, item_id: int, error_message: str) -> None:
        item = self.items[item_id]
        item["status"] = "FAILED"
        item["error_message"] = error_message

    async def update_set_counters(self, set_id: int) -> None:
        assert set_id == 10
        total = len(self.items)
        ready = len([item for item in self.items.values() if item["status"] == "READY"])
        failed = len([item for item in self.items.values() if item["status"] == "FAILED"])
        self.scene_set["item_total"] = total
        self.scene_set["item_ready"] = ready
        self.scene_set["item_failed"] = failed

    async def get_scene_set(self, set_id: int) -> dict | None:
        assert set_id == 10
        return dict(self.scene_set)

    async def mark_set_ready(self, set_id: int) -> None:
        assert set_id == 10
        self.scene_set["status"] = "READY"

    async def mark_set_failed(self, set_id: int, error_message: str) -> None:
        assert set_id == 10
        self.scene_set["status"] = "FAILED"
        self.scene_set["error_message"] = error_message


class FakeEmbeddingProvider:
    def __init__(self, vectors: list[list[float]]) -> None:
        self._vectors = vectors

    async def embed_batch(
        self,
        texts: list[str],
        instruction: str = "",
    ) -> list[list[float]]:
        del texts, instruction
        return list(self._vectors)


def _patch_config(monkeypatch: Any) -> None:
    config = SimpleNamespace(embedding_instruction_scene="scene embedding instruction")
    monkeypatch.setattr(sew, "get_config", lambda: config)


def test_embed_pending_batch_success(monkeypatch: Any) -> None:
    _patch_config(monkeypatch)
    repository = FakeSceneRepository()
    worker = SceneEmbeddingWorker(repository=cast("Any", repository), batch_size=8)
    monkeypatch.setattr(
        worker,
        "_get_embedding_provider",
        lambda: FakeEmbeddingProvider([[0.1, 0.2], [0.3, 0.4]]),
    )

    result = asyncio.run(worker.embed_pending_batch(10))
    assert result.fetched_count == 2
    assert result.marked_ready == 2
    assert result.marked_failed == 0
    assert result.pending_count == 0
    assert result.set_status == "READY"
    assert result.transitioned_ready is True
    assert repository.scene_set["status"] == "READY"


def test_embed_pending_batch_mismatch_mark_failed(
    monkeypatch: Any,
) -> None:
    _patch_config(monkeypatch)
    repository = FakeSceneRepository()
    worker = SceneEmbeddingWorker(repository=cast("Any", repository), batch_size=8)
    monkeypatch.setattr(
        worker,
        "_get_embedding_provider",
        lambda: FakeEmbeddingProvider([[0.1, 0.2]]),
    )

    result = asyncio.run(worker.embed_pending_batch(10))
    assert result.fetched_count == 2
    assert result.marked_ready == 0
    assert result.marked_failed == 2
    assert result.pending_count == 0
    assert result.set_status == "FAILED"
    assert result.transitioned_failed is True
    assert repository.scene_set["status"] == "FAILED"
