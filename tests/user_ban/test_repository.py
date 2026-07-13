"""user_ban PostgreSQL 仓储测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from komari_bot.plugins.user_ban.repository import UserBanRepository


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Connection:
    def __init__(
        self,
        *,
        fetch_results: list[list[dict[str, Any]]] | None = None,
        fetchval_results: list[object] | None = None,
    ) -> None:
        self.fetch_results = list(fetch_results or [])
        self.fetchval_results = list(fetchval_results or [])
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def fetch(self, query: str, *args: object) -> list[dict[str, Any]]:
        self.calls.append((query, args))
        return self.fetch_results.pop(0)

    async def fetchval(self, query: str, *args: object) -> object:
        self.calls.append((query, args))
        return self.fetchval_results.pop(0)


class _Acquire:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _Connection:
        return self.connection

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.closed = False

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection)

    async def close(self) -> None:
        self.closed = True


def _row(
    user_id: str,
    scope: str,
    *,
    minute: int = 0,
    reason: str | None = None,
    expires_at: datetime | None = None,
) -> dict[str, object]:
    timestamp = datetime(2026, 7, 13, 20, minute, tzinfo=UTC)
    return {
        "user_id": user_id,
        "ban_scope": scope,
        "operator_id": "42",
        "reason": reason,
        "expires_at": expires_at,
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _repository(connection: _Connection) -> tuple[UserBanRepository, _Pool]:
    repository = UserBanRepository()
    pool = _Pool(connection)
    repository._pool = pool  # type: ignore[assignment]
    return repository, pool


def test_schema_upgrades_existing_table_for_reason_and_expiry() -> None:
    sql = (
        Path(__file__).parents[2]
        / "komari_bot"
        / "plugins"
        / "user_ban"
        / "init_db.sql"
    ).read_text(encoding="utf-8")

    assert "ADD COLUMN IF NOT EXISTS reason TEXT" in sql
    assert "ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ" in sql
    assert "idx_komari_user_bans_expires_at" in sql


@pytest.mark.asyncio
async def test_add_all_scopes_is_atomic_and_returns_current_status() -> None:
    rows = [_row("10086", "chat"), _row("10086", "command")]
    connection = _Connection(fetch_results=[[], rows, rows])
    repository, _ = _repository(connection)

    kind, affected, records = await repository.add_scopes(
        user_id="10086",
        scopes=("chat", "command"),
        operator_id="42",
        reason=None,
        expires_at=None,
    )

    assert kind == "created"
    assert [record.ban_scope for record in affected] == ["chat", "command"]
    assert [record.ban_scope for record in records] == ["chat", "command"]
    assert connection.calls[1][1] == (
        "10086",
        "42",
        None,
        None,
        ["chat", "command"],
    )
    assert "ON CONFLICT" in connection.calls[1][0]


@pytest.mark.asyncio
async def test_repeated_add_overwrites_expiry_and_reason() -> None:
    expires_at = datetime.now(UTC) + timedelta(days=7)
    existing = [_row("10086", "chat")]
    updated = [_row("10086", "chat", reason="刷屏", expires_at=expires_at)]
    connection = _Connection(fetch_results=[existing, updated, updated])
    repository, _ = _repository(connection)

    kind, affected, records = await repository.add_scopes(
        user_id="10086",
        scopes=("chat",),
        operator_id="42",
        reason="刷屏",
        expires_at=expires_at,
    )

    assert kind == "updated"
    assert affected[0].reason == "刷屏"
    assert records[0].expires_at == expires_at


@pytest.mark.asyncio
async def test_repeated_identical_permanent_ban_is_idempotent() -> None:
    existing = [_row("10086", "chat")]
    connection = _Connection(fetch_results=[existing, [], existing])
    repository, _ = _repository(connection)

    kind, affected, records = await repository.add_scopes(
        user_id="10086",
        scopes=("chat",),
        operator_id="42",
        reason=None,
        expires_at=None,
    )

    assert kind == "unchanged"
    assert affected == ()
    assert records[0].ban_scope == "chat"


@pytest.mark.asyncio
async def test_remove_chat_preserves_command_scope_and_returns_removed_record() -> None:
    connection = _Connection(
        fetch_results=[
            [_row("10086", "chat", reason="刷屏")],
            [_row("10086", "command")],
        ]
    )
    repository, _ = _repository(connection)

    removed, records = await repository.remove_scopes(
        user_id="10086",
        scopes=("chat",),
    )

    assert removed[0].reason == "刷屏"
    assert [record.ban_scope for record in records] == ["command"]
    assert connection.calls[0][1] == ("10086", ["chat"])
    assert "expires_at > CURRENT_TIMESTAMP" in connection.calls[0][0]


@pytest.mark.asyncio
async def test_delete_expired_returns_full_records() -> None:
    expired_at = datetime.now(UTC) - timedelta(seconds=1)
    connection = _Connection(
        fetch_results=[[_row("10086", "chat", expires_at=expired_at)]]
    )
    repository, _ = _repository(connection)

    records = await repository.delete_expired()

    assert records[0].user_id == "10086"
    assert records[0].expires_at == expired_at
    assert "expires_at <= CURRENT_TIMESTAMP" in connection.calls[0][0]


@pytest.mark.asyncio
async def test_list_statuses_pages_users_and_returns_all_their_scopes() -> None:
    connection = _Connection(
        fetch_results=[[_row("10086", "chat"), _row("10086", "command")]],
        fetchval_results=[1],
    )
    repository, _ = _repository(connection)

    statuses, total = await repository.list_statuses(
        scope="chat",
        limit=20,
        offset=0,
    )

    assert total == 1
    assert len(statuses) == 1
    assert statuses[0].active_scopes == {"chat", "command"}
    assert connection.calls[0][1] == ("chat",)
    assert connection.calls[1][1] == ("chat", 20, 0)
    assert "CURRENT_TIMESTAMP" in connection.calls[0][0]


@pytest.mark.asyncio
async def test_close_releases_pool_reference() -> None:
    repository, pool = _repository(_Connection())

    await repository.close()

    assert pool.closed is True
    assert repository._pool is None
