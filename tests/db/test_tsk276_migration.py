"""TSK-276 Alembic 0020 receipt/fulfillment schema gate."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

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


def test_0020_is_the_single_head_after_0019() -> None:
    script = script_directory()
    assert script.get_heads() == [HEAD]
    revision = script.get_revision(HEAD)
    assert revision is not None
    assert revision.down_revision == "0019"


def test_0020_does_not_reuse_chat_reply_outbox() -> None:
    revision = script_directory().get_revision(HEAD)
    assert revision is not None
    text = Path(revision.path).read_text()
    assert "komari_chat_reply_outbox" not in text
    assert RECEIPT_TABLE in text
    assert FULFILLMENT_TABLE in text


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
            unique_key_columns = {
                str(row["column_name"])
                for row in await connection.fetch(
                    "SELECT kcu.column_name "
                    "FROM information_schema.table_constraints tc "
                    "JOIN information_schema.key_column_usage kcu "
                    "ON tc.constraint_name = kcu.constraint_name "
                    "AND tc.table_schema = kcu.table_schema "
                    "WHERE tc.table_schema = 'public' AND tc.table_name = $1 "
                    "AND tc.constraint_type = 'UNIQUE'",
                    RECEIPT_TABLE,
                )
            }
            assert {
                "app_id",
                "group_openid",
                "inbound_msg_id",
            } <= unique_key_columns

            await connection.execute(
                f"INSERT INTO {RECEIPT_TABLE} "
                "(receipt_id, app_id, group_openid, inbound_msg_id, "
                "fingerprint, result_code, reply_projection) "
                "VALUES ('receipt-1', 'app', 'group', 'msg', '{}', 'created', '{}')"
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                await connection.execute(
                    f"INSERT INTO {RECEIPT_TABLE} "
                    "(receipt_id, app_id, group_openid, inbound_msg_id, "
                    "fingerprint, result_code, reply_projection) "
                    "VALUES ('receipt-2', 'app', 'group', 'msg', '{}', 'created', '{}')"
                )
            await connection.execute(
                f"INSERT INTO {FULFILLMENT_TABLE} "
                "(receipt_id, state) VALUES ('receipt-1', 'NOT_STARTED')"
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                await connection.execute(
                    f"INSERT INTO {FULFILLMENT_TABLE} "
                    "(receipt_id, state) VALUES ('receipt-1', 'NOT_STARTED')"
                )
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await connection.execute(
                    f"INSERT INTO {FULFILLMENT_TABLE} "
                    "(receipt_id, state) VALUES ('missing-receipt', 'NOT_STARTED')"
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
