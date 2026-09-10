"""TSK-275/276 cutover guard: 0021 is the only migration head."""

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
PG_REQUIRED = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="未设置 KOMARI_TEST_POSTGRES_URL，不能执行迁移 cutover 验收",
)


def test_tsk275_does_not_create_a_parallel_alembic_head() -> None:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("version_path_separator", "os")
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["0021"]
    assert script.get_revision("0019").down_revision == "0018"  # type: ignore[union-attr]
    assert script.get_revision("0020").down_revision == "0019"  # type: ignore[union-attr]


@PG_REQUIRED
@pytest.mark.asyncio
async def test_tsk275_cutover_from_0018_keeps_prior_schema_and_reaches_one_head() -> (
    None
):
    if not same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")

    scratch = await recreate_scratch_database(f"_tsk275cutover_{uuid4().hex[:10]}")
    database_url = scratch_url(str(scratch["database"]))
    try:
        result = run_bootstrap(database_url, "upgrade", "0018")
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        result = run_bootstrap(database_url, "upgrade", "head")
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

        connection = await asyncpg.connect(**scratch)
        try:
            assert (
                await connection.fetchval("SELECT version_num FROM alembic_version")
                == "0021"
            )
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
            } <= tables
        finally:
            await connection.close()

        check = run_bootstrap(database_url, "check")
        assert check.returncode == 0, f"{check.stdout}\n{check.stderr}"
    finally:
        await drop_scratch_database(str(scratch["database"]))
