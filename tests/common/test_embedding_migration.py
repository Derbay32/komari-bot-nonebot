"""Embedding migration helper tests."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest

from komari_bot.common.embedding_migration import (
    KNOWLEDGE_MIGRATION_SPEC,
    MEMORY_MIGRATION_SPEC,
    get_pool_key,
    load_embedding_config,
    migrate_table_embeddings,
    resolve_knowledge_database_config,
    resolve_memory_database_config,
)

if TYPE_CHECKING:
    from pathlib import Path


class _FakeEmbeddingService:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return [0.1, 0.2]


class _FakeConnection:
    def __init__(
        self,
        *,
        table_exists: bool = True,
        dimension: int | None = 512,
        rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.table_exists = table_exists
        self.dimension = dimension
        self.rows = rows or []
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.updated_rows: list[tuple[str, int]] = []

    async def fetchval(self, query: str, *args: object) -> object:
        del args
        if "information_schema.tables" in query:
            return self.table_exists
        raise AssertionError

    async def fetchrow(self, query: str, table_name: str, column_name: str) -> dict[str, Any] | None:
        del table_name, column_name
        if "pg_catalog.format_type" not in query:
            raise AssertionError
        if not self.table_exists:
            return None
        if self.dimension is None:
            return {"data_type": "vector"}
        return {"data_type": f"vector({self.dimension})"}

    async def fetch(self, query: str) -> list[dict[str, Any]]:
        if "SELECT" not in query:
            raise AssertionError
        return self.rows

    async def execute(self, query: str, *args: object) -> str:
        self.executed.append((query, args))
        if "ALTER TABLE" in query and "TYPE vector(" in query:
            marker = "TYPE vector("
            dim_start = query.index(marker) + len(marker)
            dim_end = query.index(")", dim_start)
            self.dimension = int(query[dim_start:dim_end])
        if query.startswith("UPDATE "):
            row_id = args[1]
            assert isinstance(row_id, int)
            self.updated_rows.append((str(args[0]), row_id))
        return "OK"


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


def test_load_embedding_config_returns_default_when_missing(tmp_path: Path) -> None:
    config = load_embedding_config(tmp_path / "missing.json")
    assert config.embedding_dimension == 512


def test_resolve_database_config_merges_local_override(tmp_path: Path) -> None:
    shared_path = tmp_path / "database.json"
    shared_path.write_text(
        json.dumps(
            {
                "pg_host": "shared-host",
                "pg_port": 5432,
                "pg_database": "shared-db",
                "pg_user": "shared-user",
                "pg_password": "shared-pass",
            }
        ),
        encoding="utf-8",
    )
    knowledge_path = tmp_path / "knowledge.json"
    knowledge_path.write_text(
        json.dumps({"pg_database": "knowledge-db", "pg_pool_max_size": 9}),
        encoding="utf-8",
    )
    memory_path = tmp_path / "memory.json"
    memory_path.write_text(
        json.dumps({"pg_host": "memory-host", "pg_port": 15432}),
        encoding="utf-8",
    )

    knowledge_config = resolve_knowledge_database_config(
        shared_config_path=shared_path,
        knowledge_config_path=knowledge_path,
    )
    memory_config = resolve_memory_database_config(
        shared_config_path=shared_path,
        memory_config_path=memory_path,
    )

    assert knowledge_config.pg_host == "shared-host"
    assert knowledge_config.pg_database == "knowledge-db"
    assert knowledge_config.pg_pool_max_size == 9
    assert memory_config.pg_host == "memory-host"
    assert memory_config.pg_port == 15432
    assert get_pool_key(memory_config) == (
        "memory-host",
        15432,
        "shared-db",
        "shared-user",
        "shared-pass",
    )


def test_migrate_table_embeddings_supports_dry_run_without_embed_calls() -> None:
    conn = _FakeConnection(
        dimension=512,
        rows=[{"row_id": 1, "text_value": "alpha"}, {"row_id": 2, "text_value": "beta"}],
    )
    embedding_service = _FakeEmbeddingService()

    result = asyncio.run(
        migrate_table_embeddings(
            _FakePool(conn),
            spec=KNOWLEDGE_MIGRATION_SPEC,
            target_dimension=1024,
            dry_run=True,
            embedding_service=embedding_service,
        )
    )

    assert result.table_exists is True
    assert result.schema_changed is True
    assert result.row_total == 2
    assert result.updated_rows == 0
    assert embedding_service.calls == []
    assert conn.executed == []


def test_migrate_table_embeddings_rebuilds_dimension_and_index_when_needed() -> None:
    conn = _FakeConnection(
        dimension=512,
        rows=[{"row_id": 1, "text_value": "first"}, {"row_id": 2, "text_value": "second"}],
    )
    embedding_service = _FakeEmbeddingService()

    result = asyncio.run(
        migrate_table_embeddings(
            _FakePool(conn),
            spec=KNOWLEDGE_MIGRATION_SPEC,
            target_dimension=1536,
            dry_run=False,
            embedding_service=embedding_service,
        )
    )

    executed_sql = [query for query, _args in conn.executed]
    assert result.schema_changed is True
    assert result.updated_rows == 2
    assert result.failed_rows == 0
    assert conn.dimension == 1536
    assert embedding_service.calls == ["first", "second"]
    assert executed_sql[0] == 'DROP INDEX IF EXISTS "idx_komari_knowledge_embedding"'
    assert "ALTER TABLE" in executed_sql[1]
    assert executed_sql[-1].startswith("CREATE INDEX IF NOT EXISTS idx_komari_knowledge_embedding")


def test_migrate_table_embeddings_skips_missing_table() -> None:
    conn = _FakeConnection(table_exists=False)

    result = asyncio.run(
        migrate_table_embeddings(
            _FakePool(conn),
            spec=MEMORY_MIGRATION_SPEC,
            target_dimension=512,
            dry_run=False,
            embedding_service=_FakeEmbeddingService(),
        )
    )

    assert result.table_exists is False
    assert result.row_total == 0
    assert conn.executed == []


def test_migrate_table_embeddings_accepts_unbounded_vector_type() -> None:
    conn = _FakeConnection(
        dimension=None,
        rows=[{"row_id": 1, "text_value": "only"}],
    )

    result = asyncio.run(
        migrate_table_embeddings(
            _FakePool(conn),
            spec=MEMORY_MIGRATION_SPEC,
            target_dimension=2048,
            dry_run=True,
        )
    )

    assert result.current_dimension is None
    assert result.schema_changed is False


def test_migrate_table_embeddings_requires_embedding_service_when_applying() -> None:
    conn = _FakeConnection(rows=[{"row_id": 1, "text_value": "only"}])

    with pytest.raises(RuntimeError, match="需要 embedding_service"):
        asyncio.run(
            migrate_table_embeddings(
                _FakePool(conn),
                spec=MEMORY_MIGRATION_SPEC,
                target_dimension=512,
                dry_run=False,
                embedding_service=None,
            )
        )
