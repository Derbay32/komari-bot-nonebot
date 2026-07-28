"""SceneRepository schema bootstrap tests."""

from __future__ import annotations

import asyncio
from typing import Any, cast

from komari_bot.plugins.komari_decision.repositories.scene_repository import (
    SceneRepository,
)
from komari_bot.plugins.komari_decision.repositories.scene_schema import (
    SCENE_SCHEMA_STATEMENTS,
)


class _FakeConnection:
    def __init__(self) -> None:
        self.executed: list[str] = []

    async def execute(self, statement: str) -> None:
        self.executed.append(statement.strip())


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


def test_ensure_schema_uses_decision_owned_statements_once() -> None:
    conn = _FakeConnection()
    repository = SceneRepository(cast("Any", _FakePool(conn)))

    asyncio.run(repository.ensure_schema())
    asyncio.run(repository.ensure_schema())

    assert len(conn.executed) == len(SCENE_SCHEMA_STATEMENTS)
    assert conn.executed[0].startswith("CREATE TABLE IF NOT EXISTS komari_memory_scene_set")
    assert conn.executed[-1].startswith("INSERT INTO komari_memory_scene_runtime")
