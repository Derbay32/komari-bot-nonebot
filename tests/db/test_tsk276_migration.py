"""TSK-276 Alembic 0020 receipt/fulfillment schema gate."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING
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
HEAD = "0021"
#: 历史 0020 收据/履约迁移，保留其专属源文本断言（不被新 head 0021 覆盖）。
RECEIPT_REVISION = "0020"
RECEIPT_TABLE = "komari_roulette_command_receipts"
FULFILLMENT_TABLE = "komari_roulette_fulfillments"
PG_REQUIRED = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="未设置 KOMARI_TEST_POSTGRES_URL，不能执行 TSK-276 迁移验收",
)

if TYPE_CHECKING:
    from collections.abc import Mapping


def script_directory() -> ScriptDirectory:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("version_path_separator", "os")
    return ScriptDirectory.from_config(config)


def test_0020_does_not_reuse_chat_reply_outbox() -> None:
    revision = script_directory().get_revision(RECEIPT_REVISION)
    assert revision is not None
    text = Path(revision.path).read_text()
    assert "komari_chat_reply_outbox" not in text
    assert RECEIPT_TABLE in text
    assert FULFILLMENT_TABLE in text


async def _insertable_columns(
    connection: asyncpg.Connection,
    table: str,
) -> list[str]:
    """Return columns used to copy a row without inventing required values."""

    rows = await connection.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = $1 "
        "AND is_generated = 'NEVER' AND is_identity = 'NO' "
        "ORDER BY ordinal_position",
        table,
    )
    return [str(row["column_name"]) for row in rows]


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _fresh_identifier(value: object) -> object:
    """Keep the database's identifier type while making a new key."""

    if isinstance(value, UUID):
        return uuid4()
    return f"tsk276-{uuid4().hex}"


async def _copy_row_with_overrides(
    connection: asyncpg.Connection,
    table: str,
    *,
    source_column: str,
    source_value: object,
    overrides: Mapping[str, object],
) -> None:
    """Copy a known-valid row, changing only explicit key columns."""

    columns = await _insertable_columns(connection, table)
    assert source_column in columns
    assert set(overrides) <= set(columns)
    parameters: list[object] = []
    select_expressions: list[str] = []
    for column in columns:
        if column in overrides:
            parameters.append(overrides[column])
            select_expressions.append(f"${len(parameters)} AS {_quote_identifier(column)}")
        else:
            select_expressions.append(_quote_identifier(column))
    parameters.append(source_value)
    await connection.execute(
        f"INSERT INTO {_quote_identifier(table)} "
        f"({', '.join(_quote_identifier(column) for column in columns)}) "
        f"SELECT {', '.join(select_expressions)} "
        f"FROM {_quote_identifier(table)} "
        f"WHERE {_quote_identifier(source_column)} = ${len(parameters)}",
        *parameters,
    )


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

            # The service creates a complete, binding-qualified receipt and its
            # initial NOT_STARTED fulfillment.  Constraint checks below copy
            # this valid row instead of guessing values for required columns.
            from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

            from komari_bot.plugins.character_binding import BindingTransaction
            from komari_bot.plugins.komari_roulette import (
                ReplyProjection,
                RouletteCommandService,
            )
            from tests.komari_roulette.command_support import (
                command_factory,
                request,
                scope,
            )

            current = scope("migration-schema")
            scratch_engine = create_async_engine(database_url, pool_pre_ping=True)
            scratch_factory = async_sessionmaker(
                scratch_engine,
                expire_on_commit=False,
            )
            try:
                async with scratch_factory() as setup_session:
                    binding = BindingTransaction(setup_session)
                    await binding.bind(
                        app_id=current.app_id,
                        group_id=f"qq-{current.group_openid}",
                        group_openid=current.group_openid,
                        member_qq=f"qq-{current.member_openid}",
                        member_openid=current.member_openid,
                        character_name="迁移夹具",
                    )
                    await setup_session.commit()
                service = RouletteCommandService(
                    session_factory=scratch_factory,
                    reply_projector=lambda _context: ReplyProjection(
                        body="safe",
                        metadata={"test": True},
                    ),
                )
                created = await service.execute_group_command(
                    request(
                        current,
                        "migration-create",
                        command_factory("create"),
                        member_openid=current.member_openid,
                    )
                )
                assert created.receipt_id is not None
            finally:
                await scratch_engine.dispose()

            receipt_row = await connection.fetchrow(
                f"SELECT * FROM {_quote_identifier(RECEIPT_TABLE)} "
                "WHERE app_id = $1 AND group_openid = $2 AND inbound_msg_id = $3",
                current.app_id,
                current.group_openid,
                "migration-create",
            )
            assert receipt_row is not None
            receipt_key = receipt_row["receipt_id"]
            fulfillment_row = await connection.fetchrow(
                f"SELECT * FROM {_quote_identifier(FULFILLMENT_TABLE)} "
                "WHERE receipt_id = $1",
                receipt_key,
            )
            assert fulfillment_row is not None
            assert str(fulfillment_row["state"]) == "NOT_STARTED"
            assert fulfillment_row["platform_message_id"] is None

            duplicate_receipt_id = _fresh_identifier(receipt_key)
            with pytest.raises(asyncpg.UniqueViolationError):
                await _copy_row_with_overrides(
                    connection,
                    RECEIPT_TABLE,
                    source_column="receipt_id",
                    source_value=receipt_key,
                    overrides={
                        "receipt_id": duplicate_receipt_id,
                    },
                )

            second_receipt_id = _fresh_identifier(receipt_key)
            second_message_id = "migration-create-copy"
            await _copy_row_with_overrides(
                connection,
                RECEIPT_TABLE,
                source_column="receipt_id",
                source_value=receipt_key,
                overrides={
                    "receipt_id": second_receipt_id,
                    "inbound_msg_id": second_message_id,
                },
            )
            assert await _has_exact_key(connection, FULFILLMENT_TABLE, ("receipt_id",))
            await _copy_row_with_overrides(
                connection,
                FULFILLMENT_TABLE,
                source_column="receipt_id",
                source_value=receipt_key,
                overrides={"receipt_id": second_receipt_id},
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                await _copy_row_with_overrides(
                    connection,
                    FULFILLMENT_TABLE,
                    source_column="receipt_id",
                    source_value=receipt_key,
                    overrides={"receipt_id": receipt_key},
                )
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await _copy_row_with_overrides(
                    connection,
                    FULFILLMENT_TABLE,
                    source_column="receipt_id",
                    source_value=receipt_key,
                    overrides={"receipt_id": _fresh_identifier(receipt_key)},
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
