"""通过迁移命令与 PostgreSQL 验证 Jev 配置升级，不连接模型服务。"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from uuid import uuid4

import asyncpg
import pytest

from komari_bot.config.typed_config import ensure_typed_config_model
from tests.db.tsk197_gate_support import (
    POSTGRES_URL,
    drop_scratch_database,
    recreate_scratch_database,
    run_bootstrap,
    same_database,
    scratch_url,
)

SQLALCHEMY_URL = os.getenv("SQLALCHEMY_DATABASE_URL", "")
JEV_DEFAULTS = {
    "jev_base_url": "",
    "jev_api_key": "",
    "jev_model": "jev-1.14.0",
    "jev_http_timeout_seconds": 3.0,
    "jev_request_timeout_seconds": 3.0,
}


@pytest.mark.skipif(not POSTGRES_URL, reason="未设置 KOMARI_TEST_POSTGRES_URL")
@pytest.mark.parametrize("existing_row", [False, True])
@pytest.mark.asyncio
async def test_upgrade_and_downgrade_preserve_existing_config(
    *, existing_row: bool,
) -> None:
    """升级补齐默认值；降级只删除新列；已有配置、修订号与时间保持不变。"""
    if not same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")

    scratch = await recreate_scratch_database(f"_jev0022_{uuid4().hex[:10]}")
    database_url = scratch_url(str(scratch["database"]))
    try:
        result = run_bootstrap(database_url, "upgrade", "0021")
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        connection = await asyncpg.connect(**scratch)
        try:
            before_columns = {
                row["column_name"]
                for row in await connection.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' "
                    "AND table_name = 'komari_decision_config'"
                )
            }
            before = None
            if existing_row:
                schema = ensure_typed_config_model("komari_decision")
                assert schema is not None
                values = schema(
                    plugin_enable=True, bot_aliases=["Jev 迁移测试"], scene_top_k=7
                ).model_dump(mode="json")
                values.update(
                    id=1,
                    revision=17,
                    updated_at=datetime(2026, 10, 23, tzinfo=UTC).isoformat(),
                )
                # 历史表自行选择已有列，避免在用例内复制旧配置的完整结构。
                await connection.execute(
                    "INSERT INTO komari_decision_config "
                    "SELECT * FROM json_populate_record("
                    "NULL::komari_decision_config, $1::json)",
                    json.dumps(values),
                )
                row = await connection.fetchrow(
                    "SELECT * FROM komari_decision_config WHERE id = 1"
                )
                assert row is not None
                before = dict(row)
        finally:
            await connection.close()

        result = run_bootstrap(database_url, "upgrade", "head")
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        connection = await asyncpg.connect(**scratch)
        try:
            assert (
                await connection.fetchval("SELECT version_num FROM alembic_version")
                == "0022"
            )
            columns = {
                row["column_name"]: row
                for row in await connection.fetch(
                    "SELECT column_name, is_nullable, column_default "
                    "FROM information_schema.columns "
                    "WHERE table_schema = 'public' "
                    "AND table_name = 'komari_decision_config'"
                )
            }
            assert set(columns) == before_columns | JEV_DEFAULTS.keys()
            for name in JEV_DEFAULTS:
                assert columns[name]["is_nullable"] == "NO"
                assert columns[name]["column_default"] is None
            row = await connection.fetchrow(
                "SELECT * FROM komari_decision_config WHERE id = 1"
            )
            if before is None:
                assert row is None
            else:
                assert row is not None
                assert {key: row[key] for key in before} == before
                assert {key: row[key] for key in JEV_DEFAULTS} == JEV_DEFAULTS
        finally:
            await connection.close()

        result = run_bootstrap(database_url, "check")
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        result = run_bootstrap(database_url, "downgrade", "0021")
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        connection = await asyncpg.connect(**scratch)
        try:
            after_columns = {
                row["column_name"]
                for row in await connection.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' "
                    "AND table_name = 'komari_decision_config'"
                )
            }
            assert after_columns == before_columns
            row = await connection.fetchrow(
                "SELECT * FROM komari_decision_config WHERE id = 1"
            )
            assert (None if row is None else dict(row)) == before
        finally:
            await connection.close()

        result = run_bootstrap(database_url, "upgrade", "head")
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        result = run_bootstrap(database_url, "check")
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    finally:
        await drop_scratch_database(str(scratch["database"]))
