"""TSK-193 工具调用约束模式配置列的迁移链 PostgreSQL 验收。

隔离纪律：用例在从门控 DSN 派生的一次性隔离库（库名后缀
``_toolmode0014``）内重建迁移链：先 ``upgrade 0012`` 在 0012 时代
表结构上写入存量行（只显式插入当时已存在的非预算列），再依次
``upgrade 0013``（预算列以默认值补齐存量行）与 ``upgrade head``
（0014 新增的 ``agent_tool_call_mode`` 列以非空默认 ``required`` 补齐
存量行）；随后 ``downgrade 0013`` 验证列被显式回退且存量行保留，
最后回到 head 并 ``orm_bootstrap check`` 验证模型元数据零漂移。
共享门控库始终保持 head；门控用户需要 CREATEDB 权限。

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

#: 0012 时刻 komari_chat_config 的存量业务列（不含 0013 预算列与
#: 0014 新增列），供"存量行"插入使用；恰好等于
#: _NON_BUDGET_COLUMN_DEFAULTS 的键集合，与历史 schema 语义一致。
_EXPECTED_0012_VALUE_COLUMNS = tuple(sorted(_NON_BUDGET_COLUMN_DEFAULTS))


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


def _is_required_column_default(default: object) -> bool:
    """归一化判断列默认值是否为 required，不锁死 PostgreSQL 展示格式。

    PostgreSQL 会以 ``'required'::character varying`` / ``'required'::text``
    等带类型转换的形式报告默认字面量；这里只比较引号字面量部分，
    仍然确认默认值确实为 required。
    """
    if not isinstance(default, str):
        return False
    return default.split("::", 1)[0].strip() == "'required'"


async def _insert_legacy_row(connection: asyncpg.Connection) -> None:
    """在 0012 表结构上插入一行存量配置。

    按真实迁移链语义构造：只显式填充 0012 时代已存在的非预算列，
    0013 新增的预算列与 0014 新增的 agent_tool_call_mode 均留给后续
    迁移的数据库默认值补齐，不制造与历史 schema 不符的字段。
    """
    exists = await connection.fetchval(
        "SELECT EXISTS (SELECT 1 FROM komari_chat_config WHERE id = 1)"
    )
    assert not exists, "隔离库中不应存在既有配置行"
    rows = await connection.fetch(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_name = 'komari_chat_config'"
    )
    columns = {str(row["column_name"]) for row in rows}
    assert "agent_tool_call_mode" not in columns, "0012 阶段不应存在新列"
    assert not set(AGENT_BUDGET_COLUMNS) & columns, "0012 阶段不应存在预算列"
    value_columns = sorted(columns - {"id", "revision", "updated_at"})
    assert value_columns == list(_EXPECTED_0012_VALUE_COLUMNS), (
        f"0012 表列集合与预期不一致: {value_columns}"
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

    完整链路：upgrade 0012 → 在 0012 时代表结构写入存量行（仅非预算列）
    → upgrade 0013（预算列以默认值补齐存量行）→ upgrade head
    （agent_tool_call_mode 以默认值补齐存量行）→ downgrade 0013
    （列被删除且存量行仍在）→ upgrade head 后 ``orm_bootstrap check``
    零漂移。
    """
    if not _same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")

    scratch = await _recreate_scratch_database()
    scratch_url = _scratch_url(str(scratch["database"]))
    try:
        # 先停在 0012：按历史 schema 写入存量行
        result = _run_bootstrap(scratch_url, "upgrade", "0012")
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        conn = await asyncpg.connect(**scratch)
        try:
            await _insert_legacy_row(conn)
        finally:
            await conn.close()

        # upgrade 0013：预算列以数据库默认值补齐存量行
        result = _run_bootstrap(scratch_url, "upgrade", "0013")
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
            assert "agent_tool_call_mode" not in columns, "0013 阶段不应存在新列"
            assert set(AGENT_BUDGET_COLUMNS) <= columns, "0013 必须已新增预算列"
            budget_row = await conn.fetchrow(
                "SELECT agent_max_rounds, agent_max_tool_calls_per_round,"
                " agent_max_total_tool_calls FROM komari_chat_config WHERE id = 1"
            )
            assert tuple(budget_row) == AGENT_BUDGET_DEFAULTS, (
                "0013 升级必须以默认值补齐存量行"
            )
            assert await conn.fetchval(
                "SELECT revision FROM komari_chat_config WHERE id = 1"
            ) == 7, "存量行原有数据必须保留"
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
            assert _is_required_column_default(default), (
                f"默认值必须是 'required': {default!r}"
            )

            row = await conn.fetchrow(
                "SELECT agent_tool_call_mode, revision, agent_max_rounds"
                " FROM komari_chat_config WHERE id = 1"
            )
            assert row["agent_tool_call_mode"] == "required"
            assert row["revision"] == 7, "存量行原有数据必须保留"
            assert row["agent_max_rounds"] == AGENT_BUDGET_DEFAULTS[0]

            # 全新行（不显式赋值 agent_tool_call_mode）也得到默认 required；
            # 其余 NOT NULL 列无数据库默认，必须显式提供。
            explicit_values = {
                **_NON_BUDGET_COLUMN_DEFAULTS,
                "agent_max_rounds": 5,
                "agent_max_tool_calls_per_round": 2,
                "agent_max_total_tool_calls": 8,
            }
            fresh_columns = sorted(explicit_values)
            columns_sql = ", ".join(
                ["id", "revision", "updated_at", *fresh_columns]
            )
            placeholders = ", ".join(
                f"${index}" for index in range(1, 4 + len(fresh_columns))
            )
            await conn.execute(
                f"INSERT INTO komari_chat_config ({columns_sql})"
                f" VALUES ({placeholders})",
                2,
                1,
                datetime.now(UTC),
                *[explicit_values[column] for column in fresh_columns],
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
