"""EntityRepository 管理能力测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from komari_bot.plugins.komari_memory.repositories.entity_repository import (
    EntityRepository,
    UserProfileBatchUpsertError,
)


class _FakeConnection:
    def __init__(self) -> None:
        self.fetchval_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.transaction_commits = 0
        self.transaction_rollbacks = 0
        self.fail_execute_at: int | None = None
        self.fail_fetchrow_at: int | None = None
        self.return_none_fetchrow_at: int | None = None

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
        if self.fail_fetchrow_at == len(self.fetchrow_calls):
            msg = "模拟写入失败"
            raise RuntimeError(msg)
        if self.return_none_fetchrow_at == len(self.fetchrow_calls):
            return None
        return {
            "user_id": str(args[0]) if args else "u1",
            "group_id": str(args[1]) if len(args) > 1 else "g1",
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
        if self.fail_execute_at == len(self.execute_calls):
            msg = "模拟写入失败"
            raise RuntimeError(msg)
        if "DELETE" in query:
            return "DELETE 1"
        return "INSERT 0 1"

    def transaction(self) -> "_FakeTransaction":
        return _FakeTransaction(self)


class _FakeTransaction:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc, tb
        if exc_type is None:
            self._conn.transaction_commits += 1
        else:
            self._conn.transaction_rollbacks += 1


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


def test_list_user_profiles_escapes_like_wildcards() -> None:
    conn = _FakeConnection()
    repository = EntityRepository(_FakePool(conn))  # type: ignore[arg-type]

    asyncio.run(
        repository.list_user_profiles(
            limit=10,
            offset=0,
            query=r"100%_x\tag",
        )
    )

    count_query, count_args = conn.fetchval_calls[0]
    _data_query, data_args = conn.fetch_calls[0]

    assert "user_id ILIKE $1 ESCAPE '\\'" in count_query
    assert "display_name ILIKE $1 ESCAPE '\\'" in count_query
    assert "traits::text ILIKE $1 ESCAPE '\\'" in count_query
    assert count_args == (r"%100\%\_x\\tag%",)
    assert data_args == (r"%100\%\_x\\tag%", 10, 0)


def test_list_interaction_histories_escapes_like_wildcards() -> None:
    conn = _FakeConnection()
    repository = EntityRepository(_FakePool(conn))  # type: ignore[arg-type]

    asyncio.run(
        repository.list_interaction_histories(
            limit=10,
            offset=0,
            query=r"100%_x\tag",
        )
    )

    count_query, count_args = conn.fetchval_calls[0]
    _data_query, data_args = conn.fetch_calls[0]

    assert "user_id ILIKE $1 ESCAPE '\\'" in count_query
    assert "display_name ILIKE $1 ESCAPE '\\'" in count_query
    assert "file_type ILIKE $1 ESCAPE '\\'" in count_query
    assert "description ILIKE $1 ESCAPE '\\'" in count_query
    assert "summary ILIKE $1 ESCAPE '\\'" in count_query
    assert "records::text ILIKE $1 ESCAPE '\\'" in count_query
    assert count_args == (r"%100\%\_x\\tag%",)
    assert data_args == (r"%100\%\_x\\tag%", 10, 0)


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

    query, args = conn.fetchrow_calls[0]
    assert "INSERT INTO komari_memory_user_profile" in query
    assert "traits = EXCLUDED.traits" not in query
    assert "traits || $4::jsonb" in query
    assert "- $5::text[]" in query
    assert "RETURNING" in query
    assert isinstance(args[5], datetime)
    assert args[5] == datetime(2026, 4, 10, 12, 0, tzinfo=UTC)
    assert conn.transaction_commits == 1


def test_batch_upsert_user_profiles_uses_per_row_transactions() -> None:
    conn = _FakeConnection()
    repository = EntityRepository(_FakePool(conn))  # type: ignore[arg-type]

    result = asyncio.run(
        repository.batch_upsert_user_profiles(
            [
                {
                    "user_id": "u1",
                    "group_id": "g1",
                    "profile": {"display_name": "阿明", "traits": {}},
                    "importance": 4,
                },
                {
                    "user_id": "u2",
                    "group_id": "g1",
                    "profile": {"display_name": "阿华", "traits": {}},
                    "importance": 4,
                },
            ]
        )
    )

    assert len(conn.fetchrow_calls) == 2
    assert [row.user_id for row in result.upserted] == ["u1", "u2"]
    assert result.conflicts == []
    assert conn.transaction_commits == 2
    assert conn.transaction_rollbacks == 0


def test_batch_upsert_user_profiles_keeps_committed_rows_on_failure() -> None:
    conn = _FakeConnection()
    conn.fail_fetchrow_at = 2
    repository = EntityRepository(_FakePool(conn))  # type: ignore[arg-type]

    with pytest.raises(UserProfileBatchUpsertError) as exc_info:
        asyncio.run(
            repository.batch_upsert_user_profiles(
                [
                    {
                        "user_id": "u1",
                        "group_id": "g1",
                        "profile": {"display_name": "阿明", "traits": {}},
                        "importance": 4,
                    },
                    {
                        "user_id": "u2",
                        "group_id": "g1",
                        "profile": {"display_name": "阿华", "traits": {}},
                        "importance": 4,
                    },
                ]
            )
        )

    assert len(conn.fetchrow_calls) == 2
    assert [row.user_id for row in exc_info.value.upserted] == ["u1"]
    assert exc_info.value.errors[0].user_id == "u2"
    assert exc_info.value.errors[0].message == "模拟写入失败"
    assert conn.transaction_commits == 1
    assert conn.transaction_rollbacks == 1


def test_user_profile_delete_patch_uses_text_array() -> None:
    conn = _FakeConnection()
    repository = EntityRepository(_FakePool(conn))  # type: ignore[arg-type]

    asyncio.run(
        repository.batch_upsert_user_profiles(
            [
                {
                    "user_id": "u1",
                    "group_id": "g1",
                    "display_name": "阿明",
                    "set_traits": {},
                    "delete_keys": ["喜欢的食物"],
                    "updated_at": "2026-04-10T12:00:00Z",
                    "snapshot_updated_at": None,
                    "importance": 4,
                }
            ]
        )
    )

    _query, args = conn.fetchrow_calls[0]
    assert args[4] == ["喜欢的食物"]
    assert isinstance(args[5], datetime)
    assert args[5] == datetime(2026, 4, 10, 12, 0, tzinfo=UTC)


def test_user_profile_snapshot_conflict_returns_result_without_rollback() -> None:
    conn = _FakeConnection()
    conn.return_none_fetchrow_at = 1
    repository = EntityRepository(_FakePool(conn))  # type: ignore[arg-type]

    result = asyncio.run(
        repository.batch_upsert_user_profiles(
            [
                {
                    "user_id": "u1",
                    "group_id": "g1",
                    "display_name": "阿明",
                    "set_traits": {},
                    "delete_keys": [],
                    "updated_at": "2026-04-10T12:00:00Z",
                    "snapshot_updated_at": "2026-04-10T11:00:00Z",
                    "importance": 4,
                }
            ]
        )
    )

    assert result.upserted == []
    assert result.conflicts[0].user_id == "u1"
    assert result.conflicts[0].snapshot_updated_at == "2026-04-10T11:00:00Z"
    assert conn.transaction_commits == 1
    assert conn.transaction_rollbacks == 0


def test_batch_upsert_user_profiles_commits_non_conflicting_rows() -> None:
    conn = _FakeConnection()
    conn.return_none_fetchrow_at = 2
    repository = EntityRepository(_FakePool(conn))  # type: ignore[arg-type]

    result = asyncio.run(
        repository.batch_upsert_user_profiles(
            [
                {
                    "user_id": "u1",
                    "group_id": "g1",
                    "profile": {"display_name": "阿明", "traits": {}},
                    "importance": 4,
                },
                {
                    "user_id": "u2",
                    "group_id": "g1",
                    "display_name": "阿华",
                    "set_traits": {},
                    "delete_keys": [],
                    "updated_at": "2026-04-10T12:00:00Z",
                    "snapshot_updated_at": "2026-04-10T11:00:00Z",
                    "importance": 4,
                },
                {
                    "user_id": "u3",
                    "group_id": "g1",
                    "profile": {"display_name": "阿强", "traits": {}},
                    "importance": 4,
                },
            ]
        )
    )

    assert [row.user_id for row in result.upserted] == ["u1", "u3"]
    assert [conflict.user_id for conflict in result.conflicts] == ["u2"]
    assert conn.transaction_commits == 3
    assert conn.transaction_rollbacks == 0
