"""TSK-193 工具调用约束模式配置列的迁移链 PostgreSQL 验收。

隔离纪律：用例在从门控 DSN 派生的一次性隔离库（库名后缀
``_toolmode0014``）内重建迁移链：先 ``upgrade 0013`` 写入存量行，
再 ``upgrade head`` 验证 0014 新增的 ``agent_tool_call_mode`` 列以
非空默认 ``required`` 补齐存量行；随后 ``downgrade 0013`` 验证列被
显式回退且存量行保留，最后回到 head 并 ``orm_bootstrap check`` 验证
模型元数据零漂移。共享门控库始终保持 head；门控用户需要 CREATEDB 权限。

无 ``KOMARI_TEST_POSTGRES_URL`` 或与 ``SQLALCHEMY_DATABASE_URL`` 不同库
时按既有约定 skip，不硬编码凭据。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import asyncpg
import pytest

from tests.db.test_agent_budget_config_migration import (
    _NON_BUDGET_COLUMN_DEFAULTS,
    AGENT_BUDGET_COLUMNS,
    AGENT_BUDGET_DEFAULTS,
    _run_bootstrap,
    _same_database,
    _scratch_url,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")
SQLALCHEMY_URL = os.getenv("SQLALCHEMY_DATABASE_URL", "")

pytestmark = [
    pytest.mark.skipif(
        not POSTGRES_URL,
        reason="未设置 KOMARI_TEST_POSTGRES_URL，跳过迁移集成测试",
    ),
    pytest.mark.asyncio,
]

#: 0013 时刻 komari_chat_config 全量列（不含 0014 新增列），
#: 供"存量行"插入与回退验证使用。
_EXPECTED_0013_VALUE_COLUMNS = tuple(
    sorted(
        {
            "proactive_enabled",
            "proactive_cooldown",
            "proactive_max_per_hour",
            "proactive_reservation_ttl_seconds",
            "reply_fulfillment_worker_interval_seconds",
            "reply_fulfillment_batch_size",
            "reply_fulfillment_lease_seconds",
            "reply_fulfillment_max_attempts",
            "reply_fulfillment_retry_base_seconds",
            "reply_fulfillment_retry_max_seconds",
            "reply_fulfillment_tombstone_retention_days",
            "reply_fulfillment_freshness_seconds",
            *AGENT_BUDGET_COLUMNS,
        }
    )
)


def _parse_dsn(url: str) -> dict[str, Any]:
    parsed = urlparse(url.replace("postgresql+asyncpg://", "postgresql://"))
    return {
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "database": parsed.path.lstrip("/"),
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
    }


async def _recreate_scratch_database() -> dict[str, Any]:
    """重建本文件的一次性隔离库并返回其 asyncpg 连接参数。

    隔离库名 = 门控库名 + ``_toolmode0014``；先 DROP（FORCE 断开残留
    连接）再 CREATE，重复执行幂等。
    """
    base = _parse_dsn(POSTGRES_URL)
    scratch = {**base, "database": f"{base['database']}_toolmode0014"}
    connection = await asyncpg.connect(**base)
    try:
        name = str(scratch["database"]).replace('"', '""')
        await connection.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await connection.execute(f'CREATE DATABASE "{name}"')
    finally:
        await connection.close()
    return scratch


async def _drop_scratch_database(database: str) -> None:
    base = _parse_dsn(POSTGRES_URL)
    connection = await asyncpg.connect(**base)
    try:
        name = database.replace('"', '""')
        await connection.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await connection.close()


async def _insert_legacy_row(connection: asyncpg.Connection) -> None:
    """在 0013 表结构上插入一行存量配置（不包含 0014 的 agent_tool_call_mode）。"""
    exists = await connection.fetchval(
        "SELECT EXISTS (SELECT 1 FROM komari_chat_config WHERE id = 1)"
    )
    assert not exists, "隔离库中不应存在既有配置行"
    rows = await connection.fetch(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_name = 'komari_chat_config'"
    )
    columns = {str(row["column_name"]) for row in rows}
    assert "agent_tool_call_mode" not in columns, "0013 阶段不应存在新列"
    missing = [
        column for column in _EXPECTED_0013_VALUE_COLUMNS if column not in columns
    ]
    assert not missing, f"0013 表缺少列: {missing}"
    value_columns = sorted(
        set(_EXPECTED_0013_VALUE_COLUMNS) - {"id", "revision", "updated_at"}
    )
    columns_sql = ", ".join(["id", "revision", "updated_at", *value_columns])
    placeholders = ", ".join(f"${index}" for index in range(1, 4 + len(value_columns)))
    await connection.execute(
        f"INSERT INTO komari_chat_config ({columns_sql}) VALUES ({placeholders})",
        1,
        7,
        datetime.now(UTC),
        *[_NON_BUDGET_COLUMN_DEFAULTS[column] for column in value_columns],
    )


@pytest.mark.skipif(
    not POSTGRES_URL, reason="未设置 KOMARI_TEST_POSTGRES_URL，跳过迁移集成测试"
)
async def test_agent_tool_call_mode_column_preserves_rows_and_downgrades_cleanly() -> None:
    """AC1：0014 新增非空默认 required 列；升级保留存量行，downgrade 显式回退。

    完整链路：upgrade 0013 → 写入存量行 → upgrade head（新列以默认值补齐
    存量行）→ downgrade 0013（列被删除且存量行仍在）→ upgrade head 后
    ``orm_bootstrap check`` 零漂移。
    """
    if not _same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")

    scratch = await _recreate_scratch_database()
    scratch_url = _scratch_url(str(scratch["database"]))
    try:
        # 先停在 0013：写入存量行
        result = _run_bootstrap(scratch_url, "upgrade", "0013")
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        conn = await asyncpg.connect(**scratch)
        try:
            await _insert_legacy_row(conn)
        finally:
            await conn.close()

        # upgrade head：0014 新增列，存量行由数据库默认补齐
        result = _run_bootstrap(scratch_url, "upgrade", "head")
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        conn = await asyncpg.connect(**scratch)
        try:
            default = await conn.fetchval(
                "SELECT column_default FROM information_schema.columns"
                " WHERE table_name = 'komari_chat_config'"
                "   AND column_name = 'agent_tool_call_mode'"
            )
            assert default == "'required'", f"默认值必须是 'required': {default}"

            row = await conn.fetchrow(
                "SELECT agent_tool_call_mode, revision, agent_max_rounds"
                " FROM komari_chat_config WHERE id = 1"
            )
            assert row["agent_tool_call_mode"] == "required"
            assert row["revision"] == 7, "存量行原有数据必须保留"
            assert row["agent_max_rounds"] == AGENT_BUDGET_DEFAULTS[0]

            # 全新行（不显式赋值）也得到默认 required
            await conn.execute(
                "INSERT INTO komari_chat_config"
                " (id, revision, updated_at, agent_max_rounds,"
                "  agent_max_tool_calls_per_round, agent_max_total_tool_calls)"
                " VALUES (2, 1, $1, 5, 2, 8)",
                datetime.now(UTC),
            )
            fresh_row = await conn.fetchrow(
                "SELECT agent_tool_call_mode FROM komari_chat_config WHERE id = 2"
            )
            assert fresh_row["agent_tool_call_mode"] == "required"
        finally:
            await conn.close()

        # downgrade 0013：列显式回退，存量行保留
        result = _run_bootstrap(scratch_url, "downgrade", "0013")
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        conn = await asyncpg.connect(**scratch)
        try:
            columns = {
                str(row["column_name"])
                for row in await conn.fetch(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = 'komari_chat_config'"
                )
            }
            assert "agent_tool_call_mode" not in columns, "downgrade 必须删除新列"
            still_there = await conn.fetchval(
                "SELECT revision FROM komari_chat_config WHERE id = 1"
            )
            assert still_there == 7, "downgrade 不得删除存量行"
        finally:
            await conn.close()

        # 回到 head 后模型元数据与迁移链零漂移
        result = _run_bootstrap(scratch_url, "upgrade", "head")
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        check_result = _run_bootstrap(scratch_url, "check")
        assert check_result.returncode == 0, (
            f"{check_result.stdout}\n{check_result.stderr}"
        )
    finally:
        await _drop_scratch_database(str(scratch["database"]))
