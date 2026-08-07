"""MemoryService 管理能力测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

import komari_bot.plugins as plugins_package
from komari_bot.plugins.komari_memory.repositories.entity_repository import (
    UserProfileBatchUpsertResult,
)
from komari_bot.plugins.komari_memory.services.memory_service import MemoryService


class _FakeEmbeddingPlugin:
    def __init__(self) -> None:
        self.embed_calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.embed_calls.append(text)
        return [0.1, 0.2]

    def is_rerank_enabled(self) -> bool:
        return False


class _FakeConversationRepository:
    def __init__(self) -> None:
        self.created_kwargs: dict[str, Any] | None = None
        self.updated_calls: list[tuple[int, dict[str, Any]]] = []

    async def create_conversation(self, **kwargs: Any) -> dict[str, Any]:
        self.created_kwargs = dict(kwargs)
        return {
            "id": 11,
            **kwargs,
            "created_at": datetime(2026, 4, 10, 12, 0, tzinfo=UTC),
        }

    async def get_conversation(self, conversation_id: int) -> dict[str, Any] | None:
        if conversation_id != 11:
            return None
        return {
            "id": 11,
            "group_id": "g1",
            "summary": "旧总结",
            "participants": ["u1"],
            "start_time": datetime(2026, 4, 10, 10, 0, tzinfo=UTC),
            "end_time": datetime(2026, 4, 10, 11, 0, tzinfo=UTC),
            "importance_initial": 3,
            "importance_current": 3,
            "last_accessed": datetime(2026, 4, 10, 11, 0, tzinfo=UTC),
            "created_at": datetime(2026, 4, 10, 12, 0, tzinfo=UTC),
        }

    async def update_conversation(
        self,
        conversation_id: int,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.updated_calls.append((conversation_id, dict(kwargs)))
        return {"id": conversation_id, **kwargs}


class _FakeEntityRepository:
    def __init__(self) -> None:
        self.list_profile_calls: list[dict[str, Any]] = []
        self.delete_profile_calls: list[dict[str, Any]] = []
        self.upsert_profile_calls: list[dict[str, Any]] = []
        self.batch_profile_calls: list[list[dict[str, Any]]] = []

    async def list_user_profiles(self, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        self.list_profile_calls.append(dict(kwargs))
        return ([{"user_id": "u1", "group_id": "g1", "value": {"user_id": "u1"}}], 1)

    async def delete_user_profile(self, **kwargs: Any) -> bool:
        self.delete_profile_calls.append(dict(kwargs))
        return True

    async def upsert_user_profile(
        self,
        *,
        user_id: str,
        group_id: str,
        profile: dict[str, Any],
        importance: int,
    ) -> None:
        self.upsert_profile_calls.append(
            {
                "user_id": user_id,
                "group_id": group_id,
                "profile": dict(profile),
                "importance": importance,
            }
        )

    async def batch_upsert_user_profiles(
        self, payloads: list[dict[str, Any]]
    ) -> UserProfileBatchUpsertResult:
        self.batch_profile_calls.append(payloads)
        return UserProfileBatchUpsertResult()

    async def get_user_profile_row(
        self,
        *,
        user_id: str,
        group_id: str,
    ) -> dict[str, Any]:
        return {
            "user_id": user_id,
            "group_id": group_id,
            "key": "user_profile",
            "category": "profile_json",
            "importance": 4,
            "access_count": 0,
            "last_accessed": None,
            "value": {"user_id": user_id},
        }


class _FakeInteractionEventRepository:
    def __init__(self) -> None:
        self.list_calls: list[dict[str, Any]] = []
        timestamp = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)
        self.events = [
            {
                "id": 1,
                "user_id": "u1",
                "display_name": "阿明",
                "event_summary": "一起聊了游戏",
                "source_message_count": 6,
                "first_seen_at": timestamp,
                "last_seen_at": timestamp,
                "importance": 4,
                "importance_initial": 4,
                "importance_current": 0,
                "last_accessed": timestamp,
                "is_fuzzy": False,
                "created_at": timestamp,
            }
        ]

    async def list_interaction_events(
        self,
        **kwargs: Any,
    ) -> tuple[list[dict[str, Any]], int]:
        self.list_calls.append(dict(kwargs))
        return self.events, len(self.events)


def _make_service(
    *,
    monkeypatch: Any,
) -> tuple[
    MemoryService,
    _FakeConversationRepository,
    _FakeEntityRepository,
    _FakeInteractionEventRepository,
    _FakeEmbeddingPlugin,
]:
    conversation_repo = _FakeConversationRepository()
    entity_repo = _FakeEntityRepository()
    interaction_event_repo = _FakeInteractionEventRepository()
    embedding_plugin = _FakeEmbeddingPlugin()
    monkeypatch.setattr(
        plugins_package, "embedding_provider", embedding_plugin, raising=False
    )
    service = MemoryService(
        conversation_repo=cast("Any", conversation_repo),
        entity_repo=cast("Any", entity_repo),
        interaction_event_repo=cast("Any", interaction_event_repo),
    )
    return (
        service,
        conversation_repo,
        entity_repo,
        interaction_event_repo,
        embedding_plugin,
    )


def test_create_conversation_entry_embeds_summary_and_sets_defaults(
    monkeypatch: Any,
) -> None:
    service, conversation_repo, _, _, embedding_plugin = _make_service(
        monkeypatch=monkeypatch
    )

    created = asyncio.run(
        service.create_conversation_entry(
            group_id="g1",
            summary="新的对话总结",
            participants=["u1", "u2"],
            importance_initial=5,
        )
    )

    assert created["id"] == 11
    assert embedding_plugin.embed_calls == ["新的对话总结"]
    assert conversation_repo.created_kwargs is not None
    assert conversation_repo.created_kwargs["embedding"] == "[0.1, 0.2]"
    assert conversation_repo.created_kwargs["importance_current"] == 5


def test_update_conversation_entry_reembeds_when_summary_changes(
    monkeypatch: Any,
) -> None:
    service, conversation_repo, _, _, embedding_plugin = _make_service(
        monkeypatch=monkeypatch
    )

    updated = asyncio.run(
        service.update_conversation_entry(
            11,
            summary="新的总结",
            importance_current=4,
        )
    )

    assert updated is not None
    assert embedding_plugin.embed_calls == ["新的总结"]
    assert conversation_repo.updated_calls == [
        (
            11,
            {
                "group_id": None,
                "summary": "新的总结",
                "embedding": "[0.1, 0.2]",
                "participants": None,
                "start_time": None,
                "end_time": None,
                "importance_initial": None,
                "importance_current": 4,
                "last_accessed": None,
            },
        )
    ]


def test_entity_management_methods_delegate_to_repository(monkeypatch: Any) -> None:
    service, _, entity_repo, _, _ = _make_service(monkeypatch=monkeypatch)

    listed = asyncio.run(
        service.list_user_profile_rows(limit=10, offset=5, group_id="g1", query="阿明")
    )
    upserted = asyncio.run(
        service.upsert_user_profile_row(
            user_id="u1",
            group_id="g1",
            profile={"display_name": "阿明"},
            importance=5,
        )
    )
    deleted = asyncio.run(service.delete_user_profile(user_id="u1", group_id="g1"))

    assert listed[1] == 1
    assert entity_repo.list_profile_calls == [
        {"limit": 10, "offset": 5, "group_id": "g1", "user_id": None, "query": "阿明"}
    ]
    assert upserted is not None
    assert entity_repo.batch_profile_calls[0][0]["user_id"] == "u1"
    assert entity_repo.batch_profile_calls[0][0]["display_name"] == "阿明"
    assert deleted is True
    assert entity_repo.delete_profile_calls == [{"user_id": "u1", "group_id": "g1"}]


def test_list_interaction_history_rows_returns_repository_events_unchanged(
    monkeypatch: Any,
) -> None:
    service, _, _, interaction_event_repo, _ = _make_service(monkeypatch=monkeypatch)

    items, total = asyncio.run(
        service.list_interaction_history_rows(
            limit=10,
            offset=5,
            group_id="legacy-group",
            user_id="u1",
            query="游戏",
        )
    )

    assert items is interaction_event_repo.events
    assert total == 1
    assert items[0]["importance"] == 4
    assert items[0]["importance_current"] == 0
    assert items[0]["last_accessed"] == datetime(2026, 4, 10, 12, 0, tzinfo=UTC)
    assert interaction_event_repo.list_calls == [
        {"limit": 10, "offset": 5, "user_id": "u1", "query": "游戏"}
    ]
