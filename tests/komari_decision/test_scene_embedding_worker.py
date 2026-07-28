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
            1: {
                "id": 1,
                "content_text": "a",
                "status": "PENDING",
                "attempt_count": 0,
            },
            2: {
                "id": 2,
                "content_text": "b",
                "status": "PENDING",
                "attempt_count": 0,
            },
        }
        self.scene_set = {
            "id": 10,
            "status": "BUILDING",
            "item_total": 2,
            "item_ready": 0,
            "item_failed": 0,
        }

    async def claim_pending_items(
        self,
        set_id: int,
        *,
        owner_token: str,
        limit: int = 32,
        lease_seconds: int,
        max_attempts: int,
        retry_base_seconds: int,
    ) -> list[dict]:
        del lease_seconds, max_attempts, retry_base_seconds
        assert set_id == 10
        pending = [item for item in self.items.values() if item["status"] == "PENDING"]
        claimed = pending[:limit]
        for item in claimed:
            item["status"] = "PROCESSING"
            item["lease_owner"] = owner_token
            item["attempt_count"] = int(item["attempt_count"]) + 1
        return [dict(item) for item in claimed]

    async def mark_item_ready(
        self,
        item_id: int,
        owner_token: str,
        embedding: list[float],
        embedding_dim: int,
    ) -> bool:
        item = self.items[item_id]
        if item.get("lease_owner") != owner_token:
            return False
        item["status"] = "READY"
        item["embedding"] = embedding
        item["embedding_dim"] = embedding_dim
        item["lease_owner"] = None
        return True

    async def complete_item_failure(
        self,
        item_id: int,
        *,
        owner_token: str,
        error_code: str,
        error_message: str,
        max_attempts: int,
        retry_base_seconds: int,
    ) -> str:
        del retry_base_seconds
        item = self.items[item_id]
        if item.get("lease_owner") != owner_token:
            return "stale"
        item["status"] = (
            "FAILED" if int(item["attempt_count"]) >= max_attempts else "PENDING"
        )
        item["error_message"] = error_message
        item["last_error_code"] = error_code
        item["lease_owner"] = None
        return str(item["status"]).lower()

    async def refresh_set_progress(self, set_id: int) -> dict:
        assert set_id == 10
        previous_status = str(self.scene_set["status"])
        total = len(self.items)
        ready = len([item for item in self.items.values() if item["status"] == "READY"])
        failed = len([item for item in self.items.values() if item["status"] == "FAILED"])
        self.scene_set["item_total"] = total
        self.scene_set["item_ready"] = ready
        self.scene_set["item_failed"] = failed
        if ready + failed == total:
            self.scene_set["status"] = "FAILED" if failed else "READY"
        return {**self.scene_set, "previous_status": previous_status}


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
    config = SimpleNamespace(
        embedding_instruction_scene="scene embedding instruction",
        scene_embedding_lease_seconds=120,
        scene_embedding_max_attempts=3,
        scene_embedding_retry_base_seconds=30,
    )
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
    assert result.rescheduled_count == 0
    assert result.stale_count == 0
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

    first = asyncio.run(worker.embed_pending_batch(10))
    second = asyncio.run(worker.embed_pending_batch(10))
    result = asyncio.run(worker.embed_pending_batch(10))

    assert first.fetched_count == 2
    assert first.marked_ready == 0
    assert first.marked_failed == 0
    assert first.rescheduled_count == 2
    assert first.pending_count == 2
    assert first.set_status == "BUILDING"
    assert second.rescheduled_count == 2
    assert result.marked_failed == 2
    assert result.rescheduled_count == 0
    assert result.pending_count == 0
    assert result.set_status == "FAILED"
    assert result.transitioned_failed is True
    assert repository.scene_set["status"] == "FAILED"


def test_two_workers_do_not_embed_the_same_items(monkeypatch: Any) -> None:
    _patch_config(monkeypatch)
    repository = FakeSceneRepository()
    first_worker = SceneEmbeddingWorker(
        repository=cast("Any", repository),
        batch_size=8,
    )
    second_worker = SceneEmbeddingWorker(
        repository=cast("Any", repository),
        batch_size=8,
    )
    class _YieldingEmbeddingProvider(FakeEmbeddingProvider):
        async def embed_batch(
            self,
            texts: list[str],
            instruction: str = "",
        ) -> list[list[float]]:
            await asyncio.sleep(0)
            return await super().embed_batch(texts, instruction)

    provider = _YieldingEmbeddingProvider([[0.1, 0.2], [0.3, 0.4]])
    monkeypatch.setattr(first_worker, "_get_embedding_provider", lambda: provider)
    monkeypatch.setattr(second_worker, "_get_embedding_provider", lambda: provider)

    async def _run_pair() -> list[Any]:
        return list(
            await asyncio.gather(
                first_worker.embed_pending_batch(10),
                second_worker.embed_pending_batch(10),
            )
        )

    results = asyncio.run(_run_pair())

    assert sorted(result.fetched_count for result in results) == [0, 2]
    assert sum(result.marked_ready for result in results) == 2
    assert all(item["attempt_count"] == 1 for item in repository.items.values())
