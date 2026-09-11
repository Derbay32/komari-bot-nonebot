"""TSK-275/276 Alembic chain and real PostgreSQL migration gate."""

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
HEAD = "0021"
ROULETTE_TABLES = {
    "komari_roulette_games",
    "komari_roulette_players",
    "komari_roulette_results",
    "komari_roulette_result_players",
    "komari_roulette_leaderboard",
}
ROULETTE_CONFIG_TABLE = "komari_roulette_config"
ROULETTE_CONFIG_COLUMNS = (
    "id",
    "revision",
    "updated_at",
    "plugin_enable",
    "item_weight_magnifier",
    "item_weight_beer",
    "item_weight_burst",
    "item_weight_lock",
    "action_copy_pool",
    "final_copy_pool",
)

PG_REQUIRED = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="未设置 KOMARI_TEST_POSTGRES_URL，跳过迁移集成测试",
)


def _script_directory() -> ScriptDirectory:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("version_path_separator", "os")
    return ScriptDirectory.from_config(config)


def test_0021_is_the_single_head_after_roulette_config_0020() -> None:
    script = _script_directory()
    assert script.get_heads() == [HEAD]
    revision = script.get_revision(HEAD)
    assert revision is not None
    assert revision.down_revision == "0020"
    # 历史节点保留：0020 仍是 0019 的直接后继，0019 接在 0018 之后。
    assert script.get_revision("0020").down_revision == "0019"  # type: ignore[union-attr]
    assert script.get_revision("0019").down_revision == "0018"  # type: ignore[union-attr]


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
            assert tables >= ROULETTE_TABLES | {ROULETTE_CONFIG_TABLE}

            # 0021 强类型配置表字段：单行主键 + CAS revision + 空候选的
            # 四项权重与两个 JSONB 文案池（列无 server 默认，由 config_manager
            # 首次读取时按 Pydantic 默认值回写）。
            config_columns = {
                str(row["column_name"]): str(row["data_type"])
                for row in await connection.fetch(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = $1",
                    ROULETTE_CONFIG_TABLE,
                )
            }
            assert set(config_columns) == set(ROULETTE_CONFIG_COLUMNS)
            assert config_columns["action_copy_pool"] == "jsonb"
            assert config_columns["final_copy_pool"] == "jsonb"
            assert config_columns["plugin_enable"] == "boolean"
            assert config_columns["revision"] == "integer"
            # 迁移只建表，不播种配置行（运行时以 config_manager 首次读取
            # 的 Pydantic 默认值回写）。
            assert (
                await connection.fetchval(
                    f"SELECT count(*) FROM {ROULETTE_CONFIG_TABLE}"
                )
                == 0
            )
        finally:
            await connection.close()

        check = run_bootstrap(database_url, "check")
        assert check.returncode == 0, f"{check.stdout}\n{check.stderr}"
    finally:
        await drop_scratch_database(str(scratch["database"]))


@PG_REQUIRED
@pytest.mark.asyncio
async def test_upgrade_from_0018_preserves_binding_schema_and_converges_to_0021() -> (
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
