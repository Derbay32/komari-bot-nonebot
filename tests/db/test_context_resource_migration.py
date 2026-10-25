"""TSK-192 phase A: context resource ownership migration on a disposable DB."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import asyncpg
import pytest

from tests.db.tsk197_gate_support import (
    SKIP_NO_POSTGRES,
    drop_scratch_database,
    recreate_scratch_database,
    run_bootstrap,
    scratch_url,
)

pytestmark = [SKIP_NO_POSTGRES, pytest.mark.asyncio]

_MEMORY_TABLE = "komari_memory_config"
_CONTEXT_TABLE = "komari_context_config"


def _memory_value(  # noqa: PLR0911
    column: str,
    data_type: str,
    *,
    nullable: bool,
) -> Any:
    if column == "redis_db":
        return 7
    if column == "revision":
        return 77
    if column == "updated_at":
        return datetime(2026, 10, 25, tzinfo=UTC)
    if nullable:
        return None
    if data_type == "boolean":
        return False
    if data_type == "jsonb":
        return "{}"
    if data_type in {"integer", "bigint"}:
        return 1
    if data_type in {"real", "double precision"}:
        return 1.0
    if data_type.startswith("timestamp"):
        return datetime(2026, 10, 25, tzinfo=UTC)
    return ""


async def _insert_memory_row(connection: asyncpg.Connection) -> None:
    rows = await connection.fetch(
        "SELECT column_name, data_type, is_nullable "
        "FROM information_schema.columns "
        "WHERE table_name = $1 ORDER BY ordinal_position",
        _MEMORY_TABLE,
    )
    columns = [str(row["column_name"]) for row in rows]
    values = [
        _memory_value(
            str(row["column_name"]),
            str(row["data_type"]),
            nullable=str(row["is_nullable"]) == "YES",
        )
        for row in rows
    ]
    placeholders = ", ".join(f"${index}" for index in range(1, len(columns) + 1))
    await connection.execute(
        f"INSERT INTO {_MEMORY_TABLE} ({', '.join(columns)}) VALUES ({placeholders})",
        *values,
    )


async def test_context_migration_moves_only_redis_db_and_preserves_memory() -> None:
    scratch = await recreate_scratch_database("_tsk192_context")
    db_url = scratch_url(str(scratch["database"]))
    try:
        upgraded = run_bootstrap(db_url, "upgrade", "0025")
        assert upgraded.returncode == 0, upgraded.stderr

        connection = await asyncpg.connect(**scratch)
        try:
            await _insert_memory_row(connection)
            await connection.execute(
                "INSERT INTO komari_memory_conversations "
                "(group_id, summary, start_time, end_time) "
                "VALUES ('g1', 'formal memory canary', now(), now())"
            )
        finally:
            await connection.close()

        # Historical invariant at 0026's intended revision.
        result = run_bootstrap(db_url, "upgrade", "0026")
        assert result.returncode == 0, result.stderr
        connection = await asyncpg.connect(**scratch)
        try:
            context_row = await connection.fetchrow(
                f"SELECT id, revision, updated_at, redis_db "
                f"FROM {_CONTEXT_TABLE} WHERE id = 1"
            )
            assert context_row is not None
            assert context_row["redis_db"] == 7
            assert context_row["revision"] == 77
            memory_columns = {
                str(row["column_name"])
                for row in await connection.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = $1",
                    _MEMORY_TABLE,
                )
            }
            assert "redis_db" not in memory_columns
            assert (
                await connection.fetchval(
                    "SELECT summary FROM komari_memory_conversations "
                    "WHERE group_id = 'g1'"
                )
                == "formal memory canary"
            )
        finally:
            await connection.close()

        downgraded = run_bootstrap(db_url, "downgrade", "0025")
        assert downgraded.returncode == 0, downgraded.stderr
        connection = await asyncpg.connect(**scratch)
        try:
            assert await connection.fetchval(
                f"SELECT redis_db FROM {_MEMORY_TABLE} WHERE id = 1"
            ) == 7
            assert await connection.fetchval(
                "SELECT to_regclass($1) IS NULL", _CONTEXT_TABLE
            )
        finally:
            await connection.close()

        # Advance to the current head: 0030 keeps the live redis_db owner and
        # the formal source row while retiring legacy memory columns.
        result = run_bootstrap(db_url, "upgrade", "head")
        assert result.returncode == 0, result.stderr
        check = run_bootstrap(db_url, "check")
        assert check.returncode == 0, f"{check.stdout}\n{check.stderr}"
        connection = await asyncpg.connect(**scratch)
        try:
            assert await connection.fetchval(
                f"SELECT redis_db FROM {_CONTEXT_TABLE} WHERE id = 1"
            ) == 7
            memory_columns = {
                str(row["column_name"])
                for row in await connection.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = $1",
                    _MEMORY_TABLE,
                )
            }
            assert "redis_db" not in memory_columns
            assert "llm_model_summary" not in memory_columns
            assert (
                await connection.fetchval(
                    "SELECT summary FROM komari_memory_conversations "
                    "WHERE group_id = 'g1'"
                )
                == "formal memory canary"
            )
        finally:
            await connection.close()
    finally:
        await drop_scratch_database(str(scratch["database"]))
