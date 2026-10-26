"""TSK-193: Hindsight configuration migration (0028) acceptance.

The case runs in a disposable database derived from the authorized gate DSN.
It verifies the exact schema defaults for a populated singleton, zero-drift
``orm_bootstrap check`` at head, and reversible downgrade/upgrade behaviour.
"""

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

_TABLE = "komari_memory_config"
_EXISTING_SENTINEL_REVISION = 71
_UPDATED_AT = datetime(2026, 10, 25, 20, 30, tzinfo=UTC)

_HINDSIGHT_DEFAULTS: dict[str, Any] = {
    "hindsight_base_url": "",
    "hindsight_api_key": "",
    "hindsight_bank_id": "",
    "hindsight_request_timeout_seconds": 10.0,
    "hindsight_recovery_interval_seconds": 30.0,
}


def _existing_value(  # noqa: PLR0911
    column: str, data_type: str, *, nullable: bool
) -> Any:
    if nullable:
        return None
    if data_type == "boolean":
        return False
    if data_type == "jsonb":
        return "[]"
    if data_type in {"integer", "bigint"}:
        return 1
    if data_type in {"real", "double precision"}:
        return 1.0
    if data_type.startswith("timestamp"):
        return _UPDATED_AT
    if column == "llm_model_summary":
        return "migration-summary-model"
    if column == "llm_request_api_summary":
        return "chat_completions"
    if column == "response_tag":
        return "content"
    return ""


async def _insert_pre_0028_row(connection: asyncpg.Connection) -> None:
    rows = await connection.fetch(
        "SELECT column_name, data_type, is_nullable "
        "FROM information_schema.columns WHERE table_name = $1 "
        "ORDER BY ordinal_position",
        _TABLE,
    )
    values: dict[str, Any] = {}
    for row in rows:
        column = str(row["column_name"])
        if column == "id":
            values[column] = 1
        elif column == "revision":
            values[column] = _EXISTING_SENTINEL_REVISION
        elif column == "updated_at":
            values[column] = _UPDATED_AT
        else:
            values[column] = _existing_value(
                column,
                str(row["data_type"]),
                nullable=str(row["is_nullable"]) == "YES",
            )
    columns = list(values)
    placeholders = ", ".join(f"${index}" for index in range(1, len(columns) + 1))
    await connection.execute(
        f"INSERT INTO {_TABLE} ({', '.join(columns)}) VALUES ({placeholders})",
        *(values[column] for column in columns),
    )


async def _columns(connection: asyncpg.Connection) -> dict[str, dict[str, Any]]:
    rows = await connection.fetch(
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_name = $1",
        _TABLE,
    )
    return {str(row["column_name"]): dict(row) for row in rows}


async def _row(connection: asyncpg.Connection) -> dict[str, Any]:
    row = await connection.fetchrow(f"SELECT * FROM {_TABLE} WHERE id = 1")
    assert row is not None
    return dict(row)


async def test_0028_adds_defaulted_hindsight_columns_and_downgrades_cleanly() -> None:
    scratch = await recreate_scratch_database("_tsk193_hindsight_config")
    db_url = scratch_url(str(scratch["database"]))
    try:
        result = run_bootstrap(db_url, "upgrade", "0027")
        assert result.returncode == 0, result.stderr

        connection = await asyncpg.connect(**scratch)
        try:
            await _insert_pre_0028_row(connection)
        finally:
            await connection.close()

        # Historical invariant at 0028's intended revision: Hindsight
        # defaults exist and the then-surviving legacy columns are untouched.
        result = run_bootstrap(db_url, "upgrade", "0028")
        assert result.returncode == 0, result.stderr

        connection = await asyncpg.connect(**scratch)
        try:
            columns = await _columns(connection)
            row = await _row(connection)
            for column, expected in _HINDSIGHT_DEFAULTS.items():
                assert column in columns, column
                assert columns[column]["is_nullable"] == "NO", column
                assert row[column] == expected, column
            assert row["revision"] == _EXISTING_SENTINEL_REVISION
            assert row["llm_model_summary"] == "migration-summary-model"

            result = run_bootstrap(db_url, "downgrade", "0027")
            assert result.returncode == 0, result.stderr
            downgraded_columns = await _columns(connection)
            for column in _HINDSIGHT_DEFAULTS:
                assert column not in downgraded_columns, column
        finally:
            await connection.close()

        result = run_bootstrap(db_url, "upgrade", "0028")
        assert result.returncode == 0, result.stderr
        connection = await asyncpg.connect(**scratch)
        try:
            row = await _row(connection)
            for column, expected in _HINDSIGHT_DEFAULTS.items():
                assert row[column] == expected, column
        finally:
            await connection.close()

        # Advance to the current head: Hindsight values survive while 0030
        # removes the retired legacy column set.
        result = run_bootstrap(db_url, "upgrade", "head")
        assert result.returncode == 0, result.stderr
        check = run_bootstrap(db_url, "check")
        assert check.returncode == 0, f"{check.stdout}\n{check.stderr}"
        connection = await asyncpg.connect(**scratch)
        try:
            columns = await _columns(connection)
            row = await _row(connection)
            assert "llm_model_summary" not in columns
            for column, expected in _HINDSIGHT_DEFAULTS.items():
                assert row[column] == expected, column
            assert row["revision"] == _EXISTING_SENTINEL_REVISION
        finally:
            await connection.close()
    finally:
        await drop_scratch_database(str(scratch["database"]))
