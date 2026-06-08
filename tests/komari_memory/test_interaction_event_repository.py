"""InteractionEventRepository 测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from komari_bot.plugins.komari_memory.repositories.interaction_event_repository import (
    InteractionEventRepository,
)


class _FakeConnection:
    def __init__(self) -> None:
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
        self.fetch_calls.append((query, args))
        return [{"id": 1, "event_summary": "聊了轻小说", "similarity": 0.9}]

    async def fetchrow(self, query: str, *args: object) -> dict[str, object] | None:
        self.fetchrow_calls.append((query, args))
        if query.lstrip().startswith("UPDATE"):
            return {"id": 1, "event_summary": "新总结"}
        return {"id": 1, "event_summary": "旧总结"}

    async def execute(self, query: str, *args: object) -> str:
        self.execute_calls.append((query, args))
        return "DELETE 1"

    def transaction(self) -> "_FakeTransaction":
        return _FakeTransaction()


class _FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb


class _FakeAcquire:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConnection:
        return self._conn

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb


class _FakePool:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._conn)


def test_insert_interaction_event_writes_embedding_table() -> None:
    conn = _FakeConnection()
    repository = InteractionEventRepository(_FakePool(conn))  # type: ignore[arg-type]

    event_id = asyncio.run(
        repository.insert_interaction_event(
            user_id="u1",
            display_name="小鞠",
            event_summary="聊了轻小说",
            embedding="[0.1, 0.2]",
            source_message_count=3,
            first_seen_at=datetime(2026, 6, 1, tzinfo=UTC),
            last_seen_at=datetime(2026, 6, 2, tzinfo=UTC),
            importance_initial=4,
        )
    )

    assert event_id == 1
    insert_query, insert_args = conn.fetchrow_calls[0]
    assert "komari_memory_interaction_history" in insert_query
    assert "embedding" not in insert_query
    assert insert_args[3] == 3
    embedding_query, embedding_args = conn.execute_calls[0]
    assert "komari_memory_interaction_embeddings" in embedding_query
    assert embedding_args[0] == 1


def test_search_interaction_events_joins_embedding_table() -> None:
    conn = _FakeConnection()
    repository = InteractionEventRepository(_FakePool(conn))  # type: ignore[arg-type]

    results = asyncio.run(
        repository.search_interaction_events(
            user_id="u1",
            embedding="[0.1, 0.2]",
            limit=5,
        )
    )

    assert results[0]["similarity"] == 0.9
    query, args = conn.fetch_calls[0]
    assert "JOIN komari_memory_interaction_embeddings e" in query
    assert "e.embedding <=> $2::vector" in query
    assert args == ("u1", "[0.1, 0.2]", 5)


def test_update_interaction_event_upserts_embedding_table() -> None:
    conn = _FakeConnection()
    repository = InteractionEventRepository(_FakePool(conn))  # type: ignore[arg-type]

    updated = asyncio.run(
        repository.update_interaction_event(
            1,
            event_summary="新总结",
            embedding="[0.3, 0.4]",
            importance_initial=5,
        )
    )

    assert updated is not None
    update_query, update_args = conn.fetchrow_calls[0]
    assert "event_summary = $2" in update_query
    assert "embedding =" not in update_query
    assert update_args == (1, "新总结", 5)
    embedding_query, embedding_args = conn.execute_calls[0]
    assert "komari_memory_interaction_embeddings" in embedding_query
    assert embedding_args[0] == 1
