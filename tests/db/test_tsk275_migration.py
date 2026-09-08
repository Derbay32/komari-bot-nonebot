"""TSK-275 Alembic 0019 chain and real PostgreSQL migration gate."""

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
HEAD = "0019"
ROULETTE_TABLES = {
    "komari_roulette_games",
    "komari_roulette_players",
    "komari_roulette_results",
    "komari_roulette_result_players",
    "komari_roulette_leaderboard",
}

PG_REQUIRED = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="未设置 KOMARI_TEST_POSTGRES_URL，跳过迁移集成测试",
)


def _script_directory() -> ScriptDirectory:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("version_path_separator", "os")
    return ScriptDirectory.from_config(config)


def test_0019_is_the_single_head_after_character_binding_0018() -> None:
    script = _script_directory()
    assert script.get_heads() == [HEAD]
    revision = script.get_revision(HEAD)
    assert revision is not None
    assert revision.down_revision == "0018"


def test_roulette_orm_module_is_importable_without_runtime_ddl() -> None:
    """Model import is metadata registration only; Alembic owns all DDL."""

    import importlib

    module = importlib.import_module("komari_bot.plugins.komari_roulette.orm_models")
    assert {
        "komari_roulette_games",
        "komari_roulette_players",
        "komari_roulette_results",
        "komari_roulette_result_players",
        "komari_roulette_leaderboard",
    } <= {
        cls.__tablename__
        for cls in module.__dict__.values()
        if isinstance(getattr(cls, "__tablename__", None), str)
    }


@PG_REQUIRED
@pytest.mark.asyncio
async def test_fresh_database_upgrade_head_and_check_include_roulette_schema() -> None:
    if not same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")

    scratch = await recreate_scratch_database(f"_tsk275mig_{uuid4().hex[:10]}")
    database_url = scratch_url(str(scratch["database"]))
    try:
        result = run_bootstrap(database_url, "upgrade", "head")
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

        connection = await asyncpg.connect(**scratch)
        try:
            assert (
                await connection.fetchval("SELECT version_num FROM alembic_version")
                == HEAD
            )
            tables = {
                str(row["table_name"])
                for row in await connection.fetch(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            }
            assert tables >= ROULETTE_TABLES
        finally:
            await connection.close()

        check = run_bootstrap(database_url, "check")
        assert check.returncode == 0, f"{check.stdout}\n{check.stderr}"
    finally:
        await drop_scratch_database(str(scratch["database"]))


@PG_REQUIRED
@pytest.mark.asyncio
async def test_upgrade_from_0018_preserves_binding_schema_and_converges_to_0019() -> (
    None
):
    if not same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")

    scratch = await recreate_scratch_database(f"_tsk275legacy_{uuid4().hex[:10]}")
    database_url = scratch_url(str(scratch["database"]))
    try:
        at_0018 = run_bootstrap(database_url, "upgrade", "0018")
        assert at_0018.returncode == 0, f"{at_0018.stdout}\n{at_0018.stderr}"
        connection = await asyncpg.connect(**scratch)
        try:
            before = {
                str(row["table_name"])
                for row in await connection.fetch(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name LIKE 'komari_character_binding_%'"
                )
            }
        finally:
            await connection.close()
        assert {
            "komari_character_binding_groups",
            "komari_character_binding_members",
        } <= before

        at_head = run_bootstrap(database_url, "upgrade", "head")
        assert at_head.returncode == 0, f"{at_head.stdout}\n{at_head.stderr}"
        connection = await asyncpg.connect(**scratch)
        try:
            after = {
                str(row["table_name"])
                for row in await connection.fetch(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name LIKE 'komari_character_binding_%'"
                )
            }
            assert after == before
            assert (
                await connection.fetchval("SELECT version_num FROM alembic_version")
                == HEAD
            )
        finally:
            await connection.close()

        check = run_bootstrap(database_url, "check")
        assert check.returncode == 0, f"{check.stdout}\n{check.stderr}"
    finally:
        await drop_scratch_database(str(scratch["database"]))
