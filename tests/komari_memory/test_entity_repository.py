"""EntityRepository 管理能力测试。"""

from __future__ import annotations

import asyncio
from typing import Any

from komari_bot.plugins.komari_memory.repositories.entity_repository import (
    EntityRepository,
)


class _FakeConnection:
    def __init__(self) -> None:
        self.fetchval_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchval(self, query: str, *args: object) -> int:
        self.fetchval_calls.append((query, args))
        return 1

    async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
        self.fetch_calls.append((query, args))
        return [
            {
                "user_id": "u1",
                "group_id": "g1",
                "key": "user_profile",
                "category": "profile_json",
                "value": '{"user_id":"u1","traits":{"喜欢的食物":{"value":"布丁"}}}',
                "importance": 4,
                "access_count": 3,
                "last_accessed": None,
            }
        ]

    async def fetchrow(self, query: str, *args: object) -> dict[str, object] | None:
        self.fetchrow_calls.append((query, args))
        return {
            "user_id": "u1",
            "group_id": "g1",
            "key": "interaction_history",
            "category": "interaction_history",
            "value": '{"user_id":"u1","summary":"最近常聊天","records":[]}',
            "importance": 5,
            "access_count": 1,
            "last_accessed": None,
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


def test_list_user_profiles_supports_filters_and_parses_json() -> None:
    conn = _FakeConnection()
    repository = EntityRepository(_FakePool(conn))  # type: ignore[arg-type]

    items, total = asyncio.run(
        repository.list_user_profiles(
            limit=10,
            offset=5,
            group_id="g1",
            user_id="u1",
            query="布丁",
        )
    )

    count_query, count_args = conn.fetchval_calls[0]
    data_query, data_args = conn.fetch_calls[0]

    assert total == 1
    assert items[0]["value"]["traits"]["喜欢的食物"]["value"] == "布丁"
    assert "key = $1" in count_query
    assert count_args == ("user_profile", "g1", "u1", "%布丁%")
    assert "ORDER BY last_accessed DESC" in data_query
    assert data_args == ("user_profile", "g1", "u1", "%布丁%", 10, 5)


def test_get_and_delete_interaction_history_row() -> None:
    conn = _FakeConnection()
    repository = EntityRepository(_FakePool(conn))  # type: ignore[arg-type]

    row = asyncio.run(
        repository.get_interaction_history_row(user_id="u1", group_id="g1")
    )
    deleted = asyncio.run(
        repository.delete_interaction_history(user_id="u1", group_id="g1")
    )

    assert row is not None
    assert row["value"]["summary"] == "最近常聊天"
    assert deleted is True
    delete_query, delete_args = conn.execute_calls[0]
    assert "DELETE FROM komari_memory_entity" in delete_query
    assert delete_args == ("u1", "g1", "interaction_history")
