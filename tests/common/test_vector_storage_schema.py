"""Dynamic storage schema bootstrap tests."""

from __future__ import annotations

import asyncio

import pytest

from komari_bot.common.vector_storage_schema import (
    apply_schema_statements,
    build_knowledge_schema_statements,
    build_memory_schema_statements,
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


def test_build_memory_schema_statements_uses_requested_dimension() -> None:
    statements = build_memory_schema_statements(1536)
    assert "VECTOR(1536)" in statements[1]
    assert "komari_memory_entity" in statements[4]


def test_build_knowledge_schema_statements_uses_requested_dimension() -> None:
    statements = build_knowledge_schema_statements(2048)
    assert "VECTOR(2048)" in statements[1]
    assert "CREATE INDEX IF NOT EXISTS idx_komari_knowledge_embedding" in statements[2]
    assert "trigger_komari_knowledge_updated_at" in statements[-1]


def test_apply_schema_statements_executes_in_order() -> None:
    conn = _FakeConnection()

    asyncio.run(
        apply_schema_statements(
            _FakePool(conn),
            statements=("SELECT 1", "SELECT 2"),
        )
    )

    assert conn.executed == ["SELECT 1", "SELECT 2"]


def test_build_schema_statements_reject_invalid_dimension() -> None:
    with pytest.raises(ValueError, match="非法 embedding 维度"):
        build_memory_schema_statements(0)
