"""TSK-276 Alembic 0020 receipt/fulfillment schema gate."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from tests.db.tsk197_gate_support import (
    POSTGRES_URL,
    drop_scratch_database,
    recreate_scratch_database,
    run_bootstrap,
    same_database,
    scratch_url,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"
SQLALCHEMY_URL = os.getenv("SQLALCHEMY_DATABASE_URL", "")
HEAD = "0020"
RECEIPT_TABLE = "komari_roulette_command_receipts"
FULFILLMENT_TABLE = "komari_roulette_fulfillments"
PG_REQUIRED = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="未设置 KOMARI_TEST_POSTGRES_URL，不能执行 TSK-276 迁移验收",
)


def script_directory() -> ScriptDirectory:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("version_path_separator", "os")
    return ScriptDirectory.from_config(config)


def test_0020_does_not_reuse_chat_reply_outbox() -> None:
    revision = script_directory().get_revision(HEAD)
    assert revision is not None
    text = Path(revision.path).read_text()
    assert "komari_chat_reply_outbox" not in text
    assert RECEIPT_TABLE in text
    assert FULFILLMENT_TABLE in text


async def _table_columns(connection: asyncpg.Connection, table: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in await connection.fetch(
            "SELECT column_name, data_type, udt_name, is_nullable, "
            "column_default, is_identity, is_generated "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = $1 "
            "ORDER BY ordinal_position",
            table,
        )
    ]


def _coerce_value(value: object, column: dict[str, Any]) -> object:  # noqa: PLR0911
    udt_name = str(column["udt_name"])
    data_type = str(column["data_type"])
    if udt_name == "uuid":
        return value if isinstance(value, UUID) else UUID(str(value))
    if data_type in {"json", "jsonb"}:
        return value if isinstance(value, str) else json.dumps(value)
    if udt_name in {"bool", "boolean"}:
        return bool(value)
    if udt_name in {"int2", "int4", "int8", "numeric"}:
        return int(str(value))
    if udt_name in {"float4", "float8"}:
        return float(str(value))
    if udt_name in {"timestamp", "timestamptz", "date"}:
        return value if isinstance(value, datetime) else datetime.now(UTC)
    if udt_name.startswith("_"):
        return value if isinstance(value, list) else [value]
    return str(value)


def _default_required_value(column: dict[str, Any]) -> object:  # noqa: PLR0911
    name = str(column["column_name"])
    udt_name = str(column["udt_name"])
    data_type = str(column["data_type"])
    if name.endswith("_id") or name == "receipt_id":
        return uuid4() if udt_name == "uuid" else f"tsk276-{name}-{uuid4().hex}"
    if name in {"app_id", "group_openid", "member_openid", "inbound_msg_id"}:
        return f"tsk276-{name}"
    if name == "fingerprint":
        return {"command": "create", "params": {}}
    if name in {"canonical_command", "reply_projection", "metadata"}:
        return {"body": "safe", "metadata": {}}
    if name in {"state", "status"}:
        return "NOT_STARTED"
    if name == "result_code":
        return "created"
    if name in {"created_at", "updated_at", "claimed_at"}:
        return datetime.now(UTC)
    if data_type in {"jsonb", "json"}:
        return {}
    if udt_name.startswith("_"):
        return []
    if udt_name in {"bool", "boolean"}:
        return True
    if udt_name in {"int2", "int4", "int8", "numeric"}:
        return 1
    if udt_name in {"float4", "float8"}:
        return 1.0
    return f"tsk276-{name}"


async def _insert_complete_row(
    connection: asyncpg.Connection,
    table: str,
    *,
    overrides: dict[str, object],
) -> object:
    columns = await _table_columns(connection, table)
    values: dict[str, object] = {}
    for column in columns:
        name = str(column["column_name"])
        has_default = column["column_default"] is not None
        generated = str(column["is_generated"]) != "NEVER"
        identity = str(column["is_identity"]) == "YES"
        if generated or identity:
            continue
        if name in overrides:
            values[name] = _coerce_value(overrides[name], column)
        elif str(column["is_nullable"]) == "NO" and not has_default:
            values[name] = _coerce_value(_default_required_value(column), column)
    assert values, table
    quoted_columns = ", ".join(f'"{name}"' for name in values)
    placeholders = ", ".join(f"${index}" for index in range(1, len(values) + 1))
    await connection.execute(
        f'INSERT INTO "{table}" ({quoted_columns}) VALUES ({placeholders})',
        *values.values(),
    )
    return values.get("receipt_id")


async def _has_exact_unique(
    connection: asyncpg.Connection,
    table: str,
    expected: tuple[str, ...],
) -> bool:
    rows = await connection.fetch(
        "SELECT tc.constraint_name, kcu.column_name, kcu.ordinal_position "
        "FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu "
        "ON tc.constraint_name = kcu.constraint_name "
        "AND tc.table_schema = kcu.table_schema "
        "WHERE tc.table_schema = 'public' AND tc.table_name = $1 "
        "AND tc.constraint_type = 'UNIQUE' "
        "ORDER BY tc.constraint_name, kcu.ordinal_position",
        table,
    )
    grouped: dict[str, list[str]] = {}
    for row in rows:
        grouped.setdefault(str(row["constraint_name"]), []).append(
            str(row["column_name"])
        )
    if any(tuple(columns) == expected for columns in grouped.values()):
        return True
    rows = await connection.fetch(
        "SELECT array_agg(attribute.attname ORDER BY key_column.ordinality) "
        "AS columns "
        "FROM pg_class table_ref "
        "JOIN pg_namespace namespace_ref ON namespace_ref.oid = table_ref.relnamespace "
        "JOIN pg_index index_ref ON index_ref.indrelid = table_ref.oid "
        "CROSS JOIN LATERAL unnest(index_ref.indkey) WITH ORDINALITY "
        "AS key_column(attnum, ordinality) "
        "JOIN pg_attribute attribute ON attribute.attrelid = table_ref.oid "
        "AND attribute.attnum = key_column.attnum "
        "WHERE namespace_ref.nspname = 'public' AND table_ref.relname = $1 "
        "AND index_ref.indisunique AND NOT index_ref.indisprimary "
        "AND key_column.ordinality <= index_ref.indnkeyatts "
        "GROUP BY index_ref.indexrelid",
        table,
    )
    return any(tuple(row["columns"]) == expected for row in rows)


async def _has_exact_key(
    connection: asyncpg.Connection,
    table: str,
    expected: tuple[str, ...],
) -> bool:
    rows = await connection.fetch(
        "SELECT tc.constraint_name, kcu.column_name, kcu.ordinal_position "
        "FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu "
        "ON tc.constraint_name = kcu.constraint_name "
        "AND tc.table_schema = kcu.table_schema "
        "WHERE tc.table_schema = 'public' AND tc.table_name = $1 "
        "AND tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE') "
        "ORDER BY tc.constraint_name, kcu.ordinal_position",
        table,
    )
    grouped: dict[str, list[str]] = {}
    for row in rows:
        grouped.setdefault(str(row["constraint_name"]), []).append(
            str(row["column_name"])
        )
    if any(tuple(columns) == expected for columns in grouped.values()):
        return True
    rows = await connection.fetch(
        "SELECT array_agg(attribute.attname ORDER BY key_column.ordinality) "
        "AS columns "
        "FROM pg_class table_ref "
        "JOIN pg_namespace namespace_ref ON namespace_ref.oid = table_ref.relnamespace "
        "JOIN pg_index index_ref ON index_ref.indrelid = table_ref.oid "
        "CROSS JOIN LATERAL unnest(index_ref.indkey) WITH ORDINALITY "
        "AS key_column(attnum, ordinality) "
        "JOIN pg_attribute attribute ON attribute.attrelid = table_ref.oid "
        "AND attribute.attnum = key_column.attnum "
        "WHERE namespace_ref.nspname = 'public' AND table_ref.relname = $1 "
        "AND index_ref.indisunique "
        "AND key_column.ordinality <= index_ref.indnkeyatts "
        "GROUP BY index_ref.indexrelid",
        table,
    )
    return any(tuple(row["columns"]) == expected for row in rows)


@PG_REQUIRED
@pytest.mark.asyncio
async def test_fresh_database_upgrade_head_creates_receipt_and_fulfillment_schema() -> None:
    if not same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")

    scratch = await recreate_scratch_database(f"_tsk276mig_{uuid4().hex[:10]}")
    database_url = scratch_url(str(scratch["database"]))
    try:
        result = run_bootstrap(database_url, "upgrade", "head")
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        connection = await asyncpg.connect(**scratch)
        try:
            assert await connection.fetchval("SELECT version_num FROM alembic_version") == HEAD
            tables = {
                str(row["table_name"])
                for row in await connection.fetch(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            }
            assert {RECEIPT_TABLE, FULFILLMENT_TABLE} <= tables
            receipt_columns = {
                str(row["column_name"])
                for row in await connection.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = $1",
                    RECEIPT_TABLE,
                )
            }
            assert {
                "receipt_id",
                "app_id",
                "group_openid",
                "inbound_msg_id",
                "fingerprint",
                "result_code",
                "reply_projection",
            } <= receipt_columns
            fulfillment_columns = {
                str(row["column_name"])
                for row in await connection.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = $1",
                    FULFILLMENT_TABLE,
                )
            }
            assert {
                "receipt_id",
                "state",
                "platform_message_id",
            } <= fulfillment_columns
            assert await _has_exact_unique(
                connection,
                RECEIPT_TABLE,
                ("app_id", "group_openid", "inbound_msg_id"),
            )

            first_receipt_id = uuid4()
            receipt_key = await _insert_complete_row(
                connection,
                RECEIPT_TABLE,
                overrides={
                    "receipt_id": first_receipt_id,
                    "app_id": "tsk276-migration-app",
                    "group_openid": "tsk276-migration-group",
                    "inbound_msg_id": "tsk276-migration-message",
                    "fingerprint": {"command": "create", "params": {}},
                    "result_code": "created",
                    "reply_projection": {
                        "body": "safe",
                        "metadata": {"revealed": 1},
                    },
                },
            )
            assert receipt_key is not None
            with pytest.raises(asyncpg.UniqueViolationError):
                await _insert_complete_row(
                    connection,
                    RECEIPT_TABLE,
                    overrides={
                        "receipt_id": uuid4(),
                        "app_id": "tsk276-migration-app",
                        "group_openid": "tsk276-migration-group",
                        "inbound_msg_id": "tsk276-migration-message",
                        "fingerprint": {"command": "different", "params": {}},
                        "result_code": "created",
                        "reply_projection": {"body": "safe-2"},
                    },
                )
            assert await _has_exact_key(connection, FULFILLMENT_TABLE, ("receipt_id",))
            await _insert_complete_row(
                connection,
                FULFILLMENT_TABLE,
                overrides={
                    "receipt_id": receipt_key,
                    "state": "NOT_STARTED",
                    "platform_message_id": "tsk276-platform-message",
                },
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                await _insert_complete_row(
                    connection,
                    FULFILLMENT_TABLE,
                    overrides={
                        "receipt_id": receipt_key,
                        "state": "NOT_STARTED",
                        "platform_message_id": "tsk276-platform-message-2",
                    },
                )
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await _insert_complete_row(
                    connection,
                    FULFILLMENT_TABLE,
                    overrides={
                        "receipt_id": uuid4(),
                        "state": "NOT_STARTED",
                        "platform_message_id": "tsk276-platform-missing",
                    },
                )
        finally:
            await connection.close()
        check = run_bootstrap(database_url, "check")
        assert check.returncode == 0, f"{check.stdout}\n{check.stderr}"
    finally:
        await drop_scratch_database(str(scratch["database"]))


@PG_REQUIRED
@pytest.mark.asyncio
async def test_upgrade_from_0019_preserves_all_275_tables() -> None:
    if not same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")

    scratch = await recreate_scratch_database(f"_tsk276legacy_{uuid4().hex[:10]}")
    database_url = scratch_url(str(scratch["database"]))
    try:
        at_0019 = run_bootstrap(database_url, "upgrade", "0019")
        assert at_0019.returncode == 0, f"{at_0019.stdout}\n{at_0019.stderr}"
        at_head = run_bootstrap(database_url, "upgrade", "head")
        assert at_head.returncode == 0, f"{at_head.stdout}\n{at_head.stderr}"
        connection = await asyncpg.connect(**scratch)
        try:
            tables = {
                str(row["table_name"])
                for row in await connection.fetch(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            }
            assert {
                "komari_character_binding_groups",
                "komari_character_binding_members",
                "komari_roulette_games",
                "komari_roulette_players",
                "komari_roulette_results",
                "komari_roulette_result_players",
                "komari_roulette_leaderboard",
                RECEIPT_TABLE,
                FULFILLMENT_TABLE,
            } <= tables
            assert await connection.fetchval("SELECT version_num FROM alembic_version") == HEAD
        finally:
            await connection.close()
    finally:
        await drop_scratch_database(str(scratch["database"]))
