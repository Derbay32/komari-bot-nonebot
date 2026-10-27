"""TSK-207 migration seam: exact retirement, preserved values, no downgrade."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.db.test_migration_0030_hindsight_memory_retirement import (
    _insert_formal_source_rows,
    _source_index_definitions,
)
from tests.db.tsk197_gate_support import (
    SKIP_NO_POSTGRES,
    drop_scratch_database,
    recreate_scratch_database,
    run_bootstrap,
    scratch_url,
)
from tests.test_tsk207_retirement_census import (
    CHAT_RETIRED_COLUMNS,
    RETIRED_TABLES,
    SUMMARY_RETIRED_COLUMNS,
)

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "migrations/versions/0033_dsh_execution_retirement.py"
)
CHAT = "komari_chat_config"
SUMMARY = "komari_group_history_summary_config"
CODE = "0033_TSK207_IS_IRREVERSIBLE"


def test_0033_named_binds_are_noops_and_default_downgrade_refuses() -> None:
    spec = importlib.util.spec_from_file_location("migration_0033", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0033"
    assert module.down_revision == "0032"
    module.upgrade("other")
    module.downgrade("other")
    with pytest.raises(RuntimeError, match=CODE):
        module.downgrade()


async def _schema(conn: asyncpg.Connection) -> dict[str, set[str]]:
    rows = await conn.fetch(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'public'"
    )
    result: dict[str, set[str]] = {}
    for row in rows:
        result.setdefault(row["table_name"], set()).add(row["column_name"])
    return result


async def _rows(conn: asyncpg.Connection, table: str) -> list[dict[str, Any]]:
    return [dict(row) for row in await conn.fetch(f'SELECT * FROM "{table}"')]


async def _populate_singleton(conn: asyncpg.Connection, table: str) -> None:
    """Populate historical storage, using only SQL types and test-owned values."""
    exists = await conn.fetchval(f'SELECT EXISTS (SELECT 1 FROM "{table}")')
    if exists:
        await conn.execute(f'UPDATE "{table}" SET revision = 207')
        return
    columns = await conn.fetch(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = $1 "
        "AND column_default IS NULL AND is_nullable = 'NO' ORDER BY ordinal_position",
        table,
    )
    expressions = {
        "integer": "1",
        "bigint": "1",
        "boolean": "false",
        "double precision": "1.0",
        "real": "1.0",
        "jsonb": "'{}'::jsonb",
        "timestamp with time zone": "'2026-01-02T03:04:05Z'::timestamptz",
        "character varying": "'tsk207-sentinel'",
        "text": "'tsk207-sentinel'",
    }
    names = ", ".join(f'"{row["column_name"]}"' for row in columns)
    values = ", ".join(expressions[row["data_type"]] for row in columns)
    await conn.execute(f'INSERT INTO "{table}" ({names}) VALUES ({values})')
    await conn.execute(f'UPDATE "{table}" SET revision = 207')


@SKIP_NO_POSTGRES
@pytest.mark.parametrize("populated", [False, True])
async def test_0033_upgrade_exact_allowlist_and_zero_drift(*, populated: bool) -> None:
    scratch = await recreate_scratch_database(f"_tsk207_0033_{int(populated)}")
    url = scratch_url(str(scratch["database"]))
    try:
        result = run_bootstrap(url, "upgrade", "0032")
        assert result.returncode == 0, "historical upgrade failed"
        conn = await asyncpg.connect(**scratch)
        try:
            before_schema = await _schema(conn)
            if populated:
                await _insert_formal_source_rows(conn)
                for table in (
                    *sorted(RETIRED_TABLES),
                    SUMMARY,
                    "komari_prompt_komari_decision",
                    "komari_management_config",
                ):
                    await _populate_singleton(conn, table)
                await conn.execute(
                    f"UPDATE {CHAT} SET knowledge_limit = 7, context_messages_limit = 33, "
                    "agent_max_rounds = 12, agent_max_total_tool_calls = 30, "
                    "vision_image_download_max_count = 6, revision = 4207"
                )
                await conn.execute(
                    f"UPDATE {SUMMARY} SET summary_planning_round_limit = 5, "
                    "summary_lock_ttl_seconds = 411, summary_tool_scan_limit = 333, "
                    "layout_params = $1",
                    json.dumps({"canvas_width": 1777}),
                )
                await conn.execute(
                    "UPDATE komari_management_config SET api_credentials = $1",
                    json.dumps(
                        [
                            {
                                "credential_id": "sentinel",
                                "token": "test-only-value",
                                "permissions": [
                                    "config:read",
                                    "config:write",
                                    "prompt:read",
                                ],
                            }
                        ]
                    ),
                )
            source_indexes = await _source_index_definitions(conn)
            before = {
                table: await _rows(conn, table)
                for table in before_schema
                if table not in RETIRED_TABLES and table != "alembic_version"
            }
        finally:
            await conn.close()

        result = run_bootstrap(url, "upgrade", "head")
        assert result.returncode == 0, "retirement upgrade failed"
        check = run_bootstrap(url, "check")
        assert check.returncode == 0, "ORM drift after retirement"
        conn = await asyncpg.connect(**scratch)
        try:
            expected_schema = {
                table: columns
                for table, columns in before_schema.items()
                if table not in RETIRED_TABLES
            }
            expected_schema[CHAT] -= CHAT_RETIRED_COLUMNS
            expected_schema[SUMMARY] -= SUMMARY_RETIRED_COLUMNS
            assert await _source_index_definitions(conn) == source_indexes
            assert await _schema(conn) == expected_schema
            assert (
                await conn.fetchval("SELECT version_num FROM alembic_version") == "0033"
            )
            for table, rows in before.items():
                removed = (
                    CHAT_RETIRED_COLUMNS
                    if table == CHAT
                    else (SUMMARY_RETIRED_COLUMNS if table == SUMMARY else frozenset())
                )
                expected = [
                    {key: value for key, value in row.items() if key not in removed}
                    for row in rows
                ]
                assert await _rows(conn, table) == expected, table
            after = {table: await _rows(conn, table) for table in expected_schema}
        finally:
            await conn.close()
        refusal = run_bootstrap(url, "downgrade", "0032")
        assert refusal.returncode != 0
        assert CODE in refusal.stdout + refusal.stderr
        conn = await asyncpg.connect(**scratch)
        try:
            assert await _source_index_definitions(conn) == source_indexes
            assert await _schema(conn) == expected_schema
            assert {
                table: await _rows(conn, table) for table in expected_schema
            } == after
        finally:
            await conn.close()
    finally:
        await drop_scratch_database(str(scratch["database"]))
