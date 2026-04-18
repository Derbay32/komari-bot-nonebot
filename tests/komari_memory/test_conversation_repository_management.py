"""ConversationRepository 管理能力测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from komari_bot.plugins.komari_memory.repositories.conversation_repository import (
    ConversationRepository,
)


class _FakeConnection:
    def __init__(self) -> None:
        self.fetchval_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchval(self, query: str, *args: object) -> int:
        self.fetchval_calls.append((query, args))
        return 2

    async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
        self.fetch_calls.append((query, args))
        return [
            {
                "id": 11,
                "group_id": "g1",
                "summary": "foo",
                "participants": ["u1"],
                "start_time": datetime(2026, 4, 10, 10, 0, tzinfo=UTC),
                "end_time": datetime(2026, 4, 10, 11, 0, tzinfo=UTC),
                "importance_initial": 3,
                "importance_current": 3,
                "last_accessed": datetime(2026, 4, 10, 11, 0, tzinfo=UTC),
                "created_at": datetime(2026, 4, 10, 12, 0, tzinfo=UTC),
            }
        ]

    async def fetchrow(self, query: str, *args: object) -> dict[str, object] | None:
        self.fetchrow_calls.append((query, args))
        if query.lstrip().startswith("UPDATE"):
            return {
                "id": 11,
                "group_id": "g1",
                "summary": "更新后的总结",
                "participants": ["u1"],
                "start_time": datetime(2026, 4, 10, 10, 0, tzinfo=UTC),
                "end_time": datetime(2026, 4, 10, 11, 0, tzinfo=UTC),
                "importance_initial": 4,
                "importance_current": 4,
                "last_accessed": datetime(2026, 4, 10, 11, 0, tzinfo=UTC),
                "created_at": datetime(2026, 4, 10, 12, 0, tzinfo=UTC),
            }
        return {
            "id": 11,
            "group_id": "g1",
            "summary": "foo",
            "participants": ["u1"],
            "start_time": datetime(2026, 4, 10, 10, 0, tzinfo=UTC),
            "end_time": datetime(2026, 4, 10, 11, 0, tzinfo=UTC),
            "importance_initial": 3,
            "importance_current": 3,
            "last_accessed": datetime(2026, 4, 10, 11, 0, tzinfo=UTC),
            "created_at": datetime(2026, 4, 10, 12, 0, tzinfo=UTC),
        }

    async def execute(self, query: str, *args: object) -> str:
        self.execute_calls.append((query, args))
        return "DELETE 1"


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


def test_list_conversations_supports_filters_and_pagination() -> None:
    conn = _FakeConnection()
    repository = ConversationRepository(_FakePool(conn))  # type: ignore[arg-type]

    items, total = asyncio.run(
        repository.list_conversations(
            limit=10,
            offset=5,
            group_id="g1",
            participant="u1",
            query="foo",
        )
    )

    count_query, count_args = conn.fetchval_calls[0]
    data_query, data_args = conn.fetch_calls[0]

    assert total == 2
    assert items[0]["summary"] == "foo"
    assert "COUNT(*)" in count_query
    assert count_args == ("g1", "u1", "%foo%")
    assert "ORDER BY created_at DESC" in data_query
    assert data_args == ("g1", "u1", "%foo%", 10, 5)


def test_update_and_delete_conversation() -> None:
    conn = _FakeConnection()
    repository = ConversationRepository(_FakePool(conn))  # type: ignore[arg-type]

    updated = asyncio.run(
        repository.update_conversation(
            11,
            summary="更新后的总结",
            embedding="[0.1, 0.2]",
            importance_initial=4,
            importance_current=4,
        )
    )
    deleted = asyncio.run(repository.delete_conversation(11))

    assert updated is not None
    update_query, update_args = conn.fetchrow_calls[0]
    assert "summary = $2" in update_query
    assert "embedding = $3" in update_query
    assert update_args == (11, "更新后的总结", "[0.1, 0.2]", 4, 4)
    assert deleted is True
    delete_query, delete_args = conn.execute_calls[0]
    assert "DELETE FROM komari_memory_conversations" in delete_query
    assert delete_args == (11,)
