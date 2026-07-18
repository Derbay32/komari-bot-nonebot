"""MemoryService tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

from komari_bot.plugins.komari_memory.repositories.entity_repository import (
    UserProfileBatchUpsertResult,
)
from komari_bot.plugins.komari_memory.services import (
    memory_service as memory_service_module,
)
from komari_bot.plugins.komari_memory.services.memory_service import MemoryService


class _FakeConversationRepository:
    def __init__(self) -> None:
        self.insert_calls: list[dict[str, Any]] = []
        self.search_calls: list[dict[str, Any]] = []
        self.touch_calls: list[dict[str, Any]] = []

    async def insert_conversation(self, **kwargs: Any) -> int | None:
        self.insert_calls.append(kwargs)
        return 321

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


class _FakeInteractionEventRepository:
    def __init__(self) -> None:
        self.search_calls: list[dict[str, Any]] = []
        self.touch_calls: list[list[int]] = []

    async def search_interaction_events(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.search_calls.append(dict(kwargs))
        return [
            {"id": 201, "event_summary": "喜欢聊游戏", "similarity": 0.9},
            {"id": 202, "event_summary": "常常吐槽", "similarity": 0.8},
        ]

    async def touch_interaction_events(self, event_ids: list[int]) -> None:
        self.touch_calls.append(list(event_ids))


class _FakeEntityRepository:
    def __init__(self) -> None:
        self.batch_profile_calls: list[list[dict[str, Any]]] = []

    async def batch_upsert_user_profiles(
        self, payloads: list[dict[str, Any]]
    ) -> UserProfileBatchUpsertResult:
        self.batch_profile_calls.append(payloads)
        return UserProfileBatchUpsertResult()


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
            "touch_results": True,
        }
    ]
    assert repository.touch_calls == []


def test_store_conversation_passes_dedup_key_and_time_range(monkeypatch: Any) -> None:
    service, repository = _make_service(monkeypatch=monkeypatch, rerank_enabled=False)
    start_time = datetime(2026, 7, 16, 8, 0, tzinfo=UTC)
    end_time = datetime(2026, 7, 16, 9, 0, tzinfo=UTC)

    result = asyncio.run(
        service.store_conversation(
            group_id="g1",
            summary="大家聊了拉面。",
            participants=["u1"],
            importance_initial=4,
            dedup_key="dedup-1",
            start_time=start_time,
            end_time=end_time,
        )
    )

    assert result == 321
    assert repository.insert_calls == [
        {
            "group_id": "g1",
            "summary": "大家聊了拉面。",
            "embedding": "[0.1, 0.2]",
            "participants": ["u1"],
            "importance_initial": 4,
            "dedup_key": "dedup-1",
            "start_time": start_time.replace(tzinfo=None),
            "end_time": end_time.replace(tzinfo=None),
        }
    ]


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
            "touch_results": False,
        }
    ]
    assert repository.touch_calls == [
        {
            "conversation_ids": [103, 101],
        }
    ]


def test_search_interaction_events_touches_returned_events(monkeypatch: Any) -> None:
    event_repository = _FakeInteractionEventRepository()
    embedding_plugin = _FakeEmbeddingPlugin(rerank_enabled=False)
    monkeypatch.setattr(
        memory_service_module,
        "require",
        lambda _name: embedding_plugin,
    )
    service = MemoryService(
        conversation_repo=cast("Any", _FakeConversationRepository()),
        entity_repo=cast("Any", object()),
        interaction_event_repo=cast("Any", event_repository),
    )

    results = asyncio.run(
        service.search_interaction_events(user_id="u1", query="游戏", limit=2)
    )

    assert [result["id"] for result in results] == [201, 202]
    assert event_repository.search_calls == [
        {"user_id": "u1", "embedding": "[0.1, 0.2]", "limit": 2}
    ]
    assert event_repository.touch_calls == [[201, 202]]


def test_batch_upsert_user_profiles_adds_default_metadata(monkeypatch: Any) -> None:
    entity_repository = _FakeEntityRepository()
    monkeypatch.setattr(
        memory_service_module,
        "require",
        lambda _name: _FakeEmbeddingPlugin(rerank_enabled=False),
    )
    service = MemoryService(
        conversation_repo=cast("Any", _FakeConversationRepository()),
        entity_repo=cast("Any", entity_repository),
    )

    asyncio.run(
        service.batch_upsert_user_profiles(
            [
                {
                    "user_id": "u1",
                    "group_id": "g1",
                    "profile": {"display_name": "阿明", "traits": {"爱好": {"value": "游戏"}}},
                    "importance": 4,
                }
            ]
        )
    )

    payload = entity_repository.batch_profile_calls[0][0]
    assert payload["user_id"] == "u1"
    assert payload["group_id"] == "g1"
    assert payload["importance"] == 4
    assert payload["display_name"] == "阿明"
    assert payload["set_traits"] == {"爱好": {"value": "游戏"}}
    assert payload["delete_keys"] == []
    assert payload["updated_at"]
    assert payload["snapshot_updated_at"] is None
