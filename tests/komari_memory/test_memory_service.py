"""MemoryService tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from komari_bot.plugins.komari_memory.services import (
    memory_service as memory_service_module,
)
from komari_bot.plugins.komari_memory.services.memory_service import MemoryService


class _FakeConversationRepository:
    def __init__(self) -> None:
        self.search_calls: list[dict[str, Any]] = []
        self.touch_calls: list[dict[str, Any]] = []

    async def search_by_similarity(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.search_calls.append(kwargs)
        return [
            {"id": 101, "summary": "alpha", "participants": ["u1"], "similarity": 0.8},
            {"id": 102, "summary": "beta", "participants": ["u2"], "similarity": 0.7},
            {"id": 103, "summary": "gamma", "participants": ["u3"], "similarity": 0.6},
        ]

    async def touch_conversations(
        self,
        conversation_ids: list[int],
    ) -> None:
        self.touch_calls.append(
            {
                "conversation_ids": list(conversation_ids),
            }
        )


class _FakeEmbeddingPlugin:
    def __init__(self, *, rerank_enabled: bool) -> None:
        self._rerank_enabled = rerank_enabled

    async def embed(self, text: str) -> list[float]:
        del text
        return [0.1, 0.2]

    def is_rerank_enabled(self) -> bool:
        return self._rerank_enabled

    async def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_n: int,
    ) -> list[SimpleNamespace]:
        del query, documents
        return [SimpleNamespace(index=2), SimpleNamespace(index=0)][:top_n]


def _make_service(
    *,
    monkeypatch: Any,
    rerank_enabled: bool,
) -> tuple[MemoryService, _FakeConversationRepository]:
    repository = _FakeConversationRepository()
    embedding_plugin = _FakeEmbeddingPlugin(rerank_enabled=rerank_enabled)
    monkeypatch.setattr(
        memory_service_module,
        "require",
        lambda _name: embedding_plugin,
    )
    service = MemoryService(
        config=cast("Any", SimpleNamespace(forgetting_access_boost=1.2)),
        conversation_repo=cast("Any", repository),
        entity_repo=cast("Any", object()),
    )
    return service, repository


def test_search_conversations_touches_results_immediately_without_rerank(
    monkeypatch: Any,
) -> None:
    service, repository = _make_service(monkeypatch=monkeypatch, rerank_enabled=False)

    results = asyncio.run(
        service.search_conversations(
            query="hello",
            group_id="g1",
            user_id="u1",
            limit=2,
        )
    )

    assert [result["id"] for result in results] == [101, 102]
    assert repository.search_calls == [
        {
            "embedding": "[0.1, 0.2]",
            "group_id": "g1",
            "user_id": "u1",
            "limit": 2,
            "access_boost": 1.2,
            "touch_results": True,
        }
    ]
    assert repository.touch_calls == []


def test_search_conversations_only_touches_reranked_results(monkeypatch: Any) -> None:
    service, repository = _make_service(monkeypatch=monkeypatch, rerank_enabled=True)

    results = asyncio.run(
        service.search_conversations(
            query="hello",
            group_id="g1",
            user_id="u1",
            limit=2,
        )
    )

    assert [result["id"] for result in results] == [103, 101]
    assert repository.search_calls == [
        {
            "embedding": "[0.1, 0.2]",
            "group_id": "g1",
            "user_id": "u1",
            "limit": 6,
            "access_boost": 1.2,
            "touch_results": False,
        }
    ]
    assert repository.touch_calls == [
        {
            "conversation_ids": [103, 101],
            "access_boost": 1.2,
        }
    ]
