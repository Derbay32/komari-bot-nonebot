"""ConversationRepository tests."""

from __future__ import annotations

import asyncio
from typing import Any

from komari_bot.plugins.komari_memory.repositories.conversation_repository import (
    ConversationRepository,
)


class _FakeConnection:
    def __init__(self) -> None:
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *args: object) -> list[dict[str, Any]]:
        self.fetch_calls.append((query, args))
        return [
            {"id": 11, "summary": "foo", "participants": ["u1"], "similarity": 0.9},
            {"id": 12, "summary": "bar", "participants": ["u2"], "similarity": 0.8},
        ]

    async def execute(self, query: str, *args: object) -> str:
        self.execute_calls.append((query, args))
        return "UPDATE 2"


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


def test_search_by_similarity_applies_access_boost_on_hit() -> None:
    conn = _FakeConnection()
    repository = ConversationRepository(_FakePool(conn))  # type: ignore[arg-type]

    results = asyncio.run(
        repository.search_by_similarity(
            embedding="[0.1, 0.2]",
            group_id="g1",
            limit=2,
            access_boost=1.2,
        )
    )

    assert len(results) == 2
    assert len(conn.execute_calls) == 1
    update_query, update_args = conn.execute_calls[0]
    assert "importance_current = LEAST(" in update_query
    assert update_args == ([11, 12], 1.2)
