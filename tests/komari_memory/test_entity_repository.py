"""EntityRepository 管理能力测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
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
                "version": 1,
                "display_name": "阿明",
                "traits": {"喜欢的食物": {"value": "布丁"}},
                "updated_at": datetime(2026, 4, 10, 12, 0, tzinfo=UTC),
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
            "version": 1,
            "display_name": "阿明",
            "file_type": "用户的近期对鞠行为备忘录",
            "description": "会聊天",
            "summary": "最近常聊天",
            "records": [],
            "updated_at": datetime(2026, 4, 10, 12, 0, tzinfo=UTC),
            "importance": 5,
            "access_count": 1,
            "last_accessed": None,
        }

    async def execute(self, query: str, *args: object) -> str:
        self.execute_calls.append((query, args))
        if "DELETE" in query:
            return "DELETE 1"
        return "INSERT 0 1"


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


def test_list_user_profiles_supports_filters_and_parses_profile_columns() -> None:
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
    assert items[0]["key"] == "user_profile"
    assert items[0]["value"]["display_name"] == "阿明"
    assert items[0]["value"]["traits"]["喜欢的食物"]["value"] == "布丁"
    assert "FROM komari_memory_user_profile" in count_query
    assert "display_name ILIKE" in count_query
    assert count_args == ("g1", "u1", "%布丁%")
    assert "ORDER BY last_accessed DESC" in data_query
    assert data_args == ("g1", "u1", "%布丁%", 10, 5)


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
    assert row["key"] == "interaction_history"
    assert row["value"]["summary"] == "最近常聊天"
    assert deleted is True
    delete_query, delete_args = conn.execute_calls[0]
    assert "DELETE FROM komari_memory_interaction_history" in delete_query
    assert delete_args == ("u1", "g1")


def test_upsert_user_profile_normalizes_updated_at_string_to_datetime() -> None:
    conn = _FakeConnection()
    repository = EntityRepository(_FakePool(conn))  # type: ignore[arg-type]

    asyncio.run(
        repository.upsert_user_profile(
            user_id="u1",
            group_id="g1",
            profile={
                "version": 1,
                "display_name": "阿明",
                "traits": {"喜欢的食物": {"value": "布丁"}},
                "updated_at": "2026-04-10T12:00:00+00:00",
            },
            importance=4,
        )
    )

    query, args = conn.execute_calls[0]
    assert "INSERT INTO komari_memory_user_profile" in query
    assert isinstance(args[5], datetime)
    assert args[5] == datetime(2026, 4, 10, 12, 0, tzinfo=UTC)


def test_upsert_interaction_history_normalizes_updated_at_string_to_datetime() -> None:
    conn = _FakeConnection()
    repository = EntityRepository(_FakePool(conn))  # type: ignore[arg-type]

    asyncio.run(
        repository.upsert_interaction_history(
            user_id="u1",
            group_id="g1",
            interaction={
                "version": 1,
                "display_name": "阿明",
                "file_type": "用户的近期对鞠行为备忘录",
                "description": "会聊天",
                "summary": "最近常聊天",
                "records": [],
                "updated_at": "2026-04-10T12:00:00Z",
            },
            importance=5,
        )
    )

    query, args = conn.execute_calls[0]
    assert "INSERT INTO komari_memory_interaction_history" in query
    assert isinstance(args[8], datetime)
    assert args[8] == datetime(2026, 4, 10, 12, 0, tzinfo=UTC)
