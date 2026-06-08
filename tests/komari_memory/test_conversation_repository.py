"""ConversationRepository tests."""

from __future__ import annotations

import asyncio
from typing import Any

from komari_bot.plugins.komari_memory.repositories.conversation_repository import (
    ConversationRepository,
)


class _FakeConnection:
    def __init__(self, *, fetchrow_results: list[dict[str, Any] | None] | None = None) -> None:
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self._fetchrow_results = fetchrow_results or []

    async def fetch(self, query: str, *args: object) -> list[dict[str, Any]]:
        self.fetch_calls.append((query, args))
        return [
            {"id": 11, "summary": "foo", "participants": ["u1"], "similarity": 0.9},
            {"id": 12, "summary": "bar", "participants": ["u2"], "similarity": 0.8},
        ]

    async def execute(self, query: str, *args: object) -> str:
        self.execute_calls.append((query, args))
        return "UPDATE 2"

    async def fetchrow(self, query: str, *args: object) -> dict[str, Any] | None:
        self.fetchrow_calls.append((query, args))
        if self._fetchrow_results:
            return self._fetchrow_results.pop(0)
        return {"id": 1001}

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


def test_search_by_similarity_restores_initial_importance_on_hit() -> None:
    conn = _FakeConnection()
    repository = ConversationRepository(_FakePool(conn))  # type: ignore[arg-type]

    results = asyncio.run(
        repository.search_by_similarity(
            embedding="[0.1, 0.2]",
            group_id="g1",
            limit=2,
        )
    )

    assert len(results) == 2
    assert len(conn.execute_calls) == 1
    update_query, update_args = conn.execute_calls[0]
    assert "importance_current = importance_initial" in update_query
    assert update_args == ([11, 12],)


def test_search_by_similarity_can_skip_touch_results() -> None:
    conn = _FakeConnection()
    repository = ConversationRepository(_FakePool(conn))  # type: ignore[arg-type]

    results = asyncio.run(
        repository.search_by_similarity(
            embedding="[0.1, 0.2]",
            group_id="g1",
            limit=2,
            touch_results=False,
        )
    )

    assert len(results) == 2
    assert conn.execute_calls == []


def test_insert_conversation_passes_dedup_key_and_returns_id() -> None:
    conn = _FakeConnection(fetchrow_results=[{"id": 42}])
    repository = ConversationRepository(_FakePool(conn))  # type: ignore[arg-type]

    result = asyncio.run(
        repository.insert_conversation(
            group_id="g1",
            summary="大家聊了拉面。",
            embedding="[0.1, 0.2]",
            participants=["u1"],
            importance_initial=4,
            dedup_key="dedup-1",
        )
    )

    assert result == 42
    query, args = conn.fetchrow_calls[0]
    assert "dedup_key" in query
    assert "ON CONFLICT DO NOTHING" in query
    assert args[3] == "dedup-1"
    embedding_query, embedding_args = conn.execute_calls[0]
    assert "komari_memory_conversation_embeddings" in embedding_query
    assert embedding_args[0] == 42


def test_insert_conversation_returns_none_on_dedup_conflict() -> None:
    conn = _FakeConnection(fetchrow_results=[None])
    repository = ConversationRepository(_FakePool(conn))  # type: ignore[arg-type]

    result = asyncio.run(
        repository.insert_conversation(
            group_id="g1",
            summary="大家聊了拉面。",
            embedding="[0.1, 0.2]",
            participants=["u1"],
            importance_initial=4,
            dedup_key="dedup-1",
        )
    )

    assert result is None


def test_insert_conversation_allows_none_dedup_key() -> None:
    conn = _FakeConnection(fetchrow_results=[{"id": 43}, {"id": 44}])
    repository = ConversationRepository(_FakePool(conn))  # type: ignore[arg-type]

    first_id = asyncio.run(
        repository.insert_conversation(
            group_id="g1",
            summary="第一条旧路径总结。",
            embedding="[0.1, 0.2]",
            participants=["u1"],
            importance_initial=3,
        )
    )
    second_id = asyncio.run(
        repository.insert_conversation(
            group_id="g1",
            summary="第二条旧路径总结。",
            embedding="[0.1, 0.2]",
            participants=["u1"],
            importance_initial=3,
        )
    )

    assert (first_id, second_id) == (43, 44)
    assert [call[1][3] for call in conn.fetchrow_calls] == [None, None]
    assert all(
        "komari_memory_conversation_embeddings" in call[0]
        for call in conn.execute_calls
    )
