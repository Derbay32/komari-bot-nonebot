"""TSK-192 回复 Agent 执行预算配置列的迁移链 PostgreSQL 验收。

隔离纪律：用例在从门控 DSN 派生的一次性隔离库（库名后缀
``_budget0013``）内重建迁移链，``upgrade head`` 后验证 0013 引入的
三项预算列（agent_max_rounds / agent_max_tool_calls_per_round /
agent_max_total_tool_calls）的默认值、跨字段 CHECK 约束与零漂移
（``orm_bootstrap check``），用例结束即 DROP。共享门控库始终保持
head；门控用户需要 CREATEDB 权限。

无 ``KOMARI_TEST_POSTGRES_URL`` 或与 ``SQLALCHEMY_DATABASE_URL`` 不同库
时按既有约定 skip，不硬编码凭据。
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import asyncpg
import pytest
from asyncpg.exceptions import CheckViolationError

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

AGENT_BUDGET_COLUMNS = (
    "agent_max_rounds",
    "agent_max_tool_calls_per_round",
    "agent_max_total_tool_calls",
)
AGENT_BUDGET_DEFAULTS = (10, 4, 20)

#: 除预算列外 komari_chat_config 现有列的默认值（与 schema 与迁移链一致），
#: 用于构造"只让预算列走数据库默认值"的测试行。
_NON_BUDGET_COLUMN_DEFAULTS: dict[str, object] = {
    "proactive_enabled": False,
    "proactive_cooldown": 300,
    "proactive_max_per_hour": 400,
    "proactive_reservation_ttl_seconds": 360,
    "reply_fulfillment_worker_interval_seconds": 5,
    "reply_fulfillment_batch_size": 20,
    "reply_fulfillment_lease_seconds": 120,
    "reply_fulfillment_max_attempts": 20,
    "reply_fulfillment_retry_base_seconds": 5,
    "reply_fulfillment_retry_max_seconds": 3600,
    "reply_fulfillment_tombstone_retention_days": 30,
    "reply_fulfillment_freshness_seconds": 120,
    # TSK-193：0014 新增的非空默认列（head 下插入“只让预算列走默认”
    # 的行时必须显式提供或接受默认；纳入字典以保持 head 集成测试合法）
    "agent_tool_call_mode": "required",
    # TSK-194：0015 新增的图片理解模式与 8 项下载预算（head 下插入
    # “只让预算列走默认”的行时必须显式提供或接受默认；纳入字典以保持
    # head 集成测试合法；其中 image_understanding_mode 为非空默认列）
    "image_understanding_mode": "delegated",
    "vision_image_download_max_count": 4,
    "vision_image_download_max_bytes": 8 * 1024 * 1024,
    "vision_image_download_total_max_bytes": 20 * 1024 * 1024,
    "vision_image_download_max_pixels": 40_000_000,
    "vision_image_download_concurrency": 2,
    "vision_image_download_connect_timeout_seconds": 5.0,
    "vision_image_download_read_timeout_seconds": 30.0,
    "vision_image_download_total_timeout_seconds": 45.0,
}


def _same_database(left: str, right: str) -> bool:
    left_parsed = urlparse(left)
    right_parsed = urlparse(right)
    return (
        left_parsed.hostname == right_parsed.hostname
        and (left_parsed.port or 5432) == (right_parsed.port or 5432)
        and left_parsed.path == right_parsed.path
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


def _run_bootstrap(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["SQLALCHEMY_DATABASE_URL"] = url
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "komari_bot.db.orm_bootstrap", *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


def _scratch_url(database: str) -> str:
    """把门控 DSN 的库名替换为隔离库名，其余连接参数保持不变。"""
    return urlparse(POSTGRES_URL)._replace(path=f"/{database}").geturl()


async def _recreate_scratch_database() -> dict[str, Any]:
    """重建本文件的一次性隔离库并返回其 asyncpg 连接参数。

    隔离库名 = 门控库名 + ``_budget0013``；先 DROP（FORCE 断开残留
    连接）再 CREATE，重复执行幂等。
    """
    base = _parse_dsn(POSTGRES_URL)
    scratch = {**base, "database": f"{base['database']}_budget0013"}
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


async def _prepare_head_scratch() -> tuple[dict[str, Any], str]:
    """重建隔离库并 upgrade head；head 必须包含 0013 预算列迁移。"""
    scratch = await _recreate_scratch_database()
    scratch_url = _scratch_url(str(scratch["database"]))
    result = _run_bootstrap(scratch_url, "upgrade", "head")
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    return scratch, scratch_url


async def _insert_row_without_budget_columns(
    connection: asyncpg.Connection,
    *,
    revision: int = 1,
) -> None:
    """插入一行配置，只显式填充非预算列，让 0013 的列默认值生效。

    ``revision`` 默认 1，保持既有调用语义；历史阶段（0015 之前）调用方
    显式传入存量 revision，用于验证迁移不得触碰 CAS revision。
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
    value_columns = sorted(
        columns - {"id", "revision", "updated_at"} - set(AGENT_BUDGET_COLUMNS)
    )
    missing = [
        column for column in value_columns if column not in _NON_BUDGET_COLUMN_DEFAULTS
    ]
    assert not missing, f"测试默认值字典缺少列: {missing}"
    columns_sql = ", ".join(["id", "revision", "updated_at", *value_columns])
    placeholders = ", ".join(f"${index}" for index in range(1, 4 + len(value_columns)))
    await connection.execute(
        f"INSERT INTO komari_chat_config ({columns_sql}) VALUES ({placeholders})",
        1,
        revision,
        datetime.now(UTC),
        *[_NON_BUDGET_COLUMN_DEFAULTS[column] for column in value_columns],
    )


@pytest.mark.skipif(
    not POSTGRES_URL, reason="未设置 KOMARI_TEST_POSTGRES_URL，跳过迁移集成测试"
)
async def test_agent_budget_columns_defaults_check_constraint_and_zero_drift() -> None:
    """AC1/AC2：预算列默认值、范围与跨字段 CHECK 约束在真实 PG 生效且零漂移。"""
    if not _same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")

    scratch, scratch_url = await _prepare_head_scratch()
    try:
        conn = await asyncpg.connect(**scratch)
        try:
            # 列存在且默认值正确（字符串形式的 PG 默认字面量）
            rows = await conn.fetch(
                "SELECT column_name, column_default"
                " FROM information_schema.columns"
                " WHERE table_name = 'komari_chat_config'"
                "   AND column_name = ANY($1::text[])",
                list(AGENT_BUDGET_COLUMNS),
            )
            defaults = {str(row["column_name"]): row["column_default"] for row in rows}
            assert set(defaults) == set(AGENT_BUDGET_COLUMNS)
            assert defaults == {
                "agent_max_rounds": "10",
                "agent_max_tool_calls_per_round": "4",
                "agent_max_total_tool_calls": "20",
            }

            # 存在同时引用三项预算列的 CHECK 约束（跨字段语义）
            constraint_rows = await conn.fetch(
                "SELECT pg_get_constraintdef(oid) AS definition"
                " FROM pg_constraint"
                " WHERE conrelid = 'komari_chat_config'::regclass"
                "   AND contype = 'c'"
            )
            cross_field_constraints = [
                str(row["definition"])
                for row in constraint_rows
                if all(
                    column in str(row["definition"]) for column in AGENT_BUDGET_COLUMNS
                )
            ]
            assert cross_field_constraints, (
                "komari_chat_config 缺少同时引用三项预算列的 CHECK 约束"
            )

            # 全新行使用默认值 10/4/20（预算列不显式赋值，验证 DB 默认生效）
            await _insert_row_without_budget_columns(conn)
            defaults_row = await conn.fetchrow(
                "SELECT agent_max_rounds, agent_max_tool_calls_per_round,"
                " agent_max_total_tool_calls FROM komari_chat_config WHERE id = 1"
            )
            assert tuple(defaults_row) == AGENT_BUDGET_DEFAULTS

            # 跨字段非法组合被数据库 CHECK 拒绝
            for set_clause in (
                # 单轮预算 > 总预算
                "agent_max_tool_calls_per_round = 5, agent_max_total_tool_calls = 4",
                # 总预算 > 轮次 x 单轮预算
                "agent_max_rounds = 2,"
                " agent_max_tool_calls_per_round = 4,"
                " agent_max_total_tool_calls = 9",
            ):
                with pytest.raises(CheckViolationError):
                    await conn.execute(
                        f"UPDATE komari_chat_config SET {set_clause} WHERE id = 1"
                    )

            # 相等边界组合合法且不破坏后续检查
            await conn.execute(
                "UPDATE komari_chat_config SET"
                " agent_max_rounds = 2,"
                " agent_max_tool_calls_per_round = 4,"
                " agent_max_total_tool_calls = 8"
                " WHERE id = 1"
            )
            boundary_row = await conn.fetchrow(
                "SELECT agent_max_rounds, agent_max_tool_calls_per_round,"
                " agent_max_total_tool_calls FROM komari_chat_config WHERE id = 1"
            )
            assert tuple(boundary_row) == (2, 4, 8)
        finally:
            await conn.close()

        # 模型元数据与迁移链零漂移
        check_result = _run_bootstrap(scratch_url, "check")
        assert check_result.returncode == 0, (
            f"{check_result.stdout}\n{check_result.stderr}"
        )
    finally:
        await _drop_scratch_database(str(scratch["database"]))
