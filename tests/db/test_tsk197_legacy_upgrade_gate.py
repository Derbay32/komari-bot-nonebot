"""TSK-197 验收 2/3 —— 带旧配置数据库的协调式升级门禁（PostgreSQL 集成）。

本文件是 TSK-197（最终 integrate-and-verify gate）的协调式旧库升级
验收：在同一个一次性隔离库内，从旧 revision（0011）构造同时携带「旧聊天
Prompt（含 ``output_instruction`` 自定义值）+ 旧图片开关（
``vision_tool_enabled``）+ 自定义图片下载预算 + 旧 chat 配置存量行」的
数据库，一次性 ``upgrade head`` 到 0015，随后播种并校验：

- 旧聊天 Prompt 的 ``output_instruction`` 列被显式删除，7 个新行为列就位；
  被删除的自定义内容绝不并入任何新字段（TSK-190 契约）；
- 旧图片开关按 bool 双分支精确迁移（false → ``native``）且 8 项预算原样
  复制到 ``komari_chat_config``；memory 的 9 个旧图片列被删除（不留 alias /
  双读 / fallback）；chat 存量 revision 不被迁移触碰；
- 0013 预算列 / 0014 工具约束列 / 0015 图片列以数据库默认值补齐存量行
  （``agent_tool_call_mode`` = required、预算 = 10/4/20）；
- 播种补齐新 Prompt 行为字段且只补空字段、绝不覆盖非空自定义值，重复播种
  幂等；
- ``orm_bootstrap check`` 模型元数据零漂移。

隔离纪律：用例在从门控 DSN 派生的一次性隔离库（库名后缀
``_tsk197legacy``）内重建迁移链，用例结束即 DROP；共享门控库始终保持
head。门控用户需要 CREATEDB 权限。无 ``KOMARI_TEST_POSTGRES_URL`` 或与
``SQLALCHEMY_DATABASE_URL`` 不同库时按既有约定 skip，不硬编码凭据。
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
import yaml

from komari_bot.db.seed_bootstrap import DEFAULT_SEED_FILE
from tests.config.chat_prompt_field_contract import LEGACY_CHAT_COLUMNS
from tests.config.prompt_field_contract import (
    find_prompt_mapping,
    prompt_resource_field_names,
    prompt_table_name,
)
from tests.db.test_agent_budget_config_migration import (
    _insert_row_without_budget_columns,
    _run_bootstrap,
    _same_database,
    _scratch_url,
)
from tests.db.test_image_understanding_mode_migration import (
    _BUDGET_COLUMNS,
    _CHAT_LEGACY_REVISION,
    _MEMORY_LEGACY_REVISION,
    MEMORY_LEGACY_IMAGE_COLUMNS,
    MEMORY_VISION_VALUES,
    _memory_row_value,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")
SQLALCHEMY_URL = os.getenv("SQLALCHEMY_DATABASE_URL", "")

pytestmark = [
    pytest.mark.skipif(
        not POSTGRES_URL,
        reason="未设置 KOMARI_TEST_POSTGRES_URL，跳过集成测试",
    ),
    pytest.mark.asyncio,
]

HEAD_REVISION = "0015"
LEGACY_REVISION = "0011"
CHAT_TABLE = "komari_chat_config"
MEMORY_TABLE = "komari_memory_config"
PROMPT_TABLE = "komari_prompt_komari_chat"

#: 旧库自定义 output_instruction 值：升级后必须消失且不并入任何字段。
CUSTOM_OUTPUT_MARKER = "CUSTOM-OUTPUT-MARKER-tsk197"


def _parse_dsn(url: str) -> dict[str, Any]:
    parsed = urlparse(url.replace("postgresql+asyncpg://", "postgresql://"))
    return {
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "database": parsed.path.lstrip("/"),
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
    }


def _run_seed(url: str) -> subprocess.CompletedProcess[str]:
    """以公开 CLI 形式执行播种命令（与 prestart / 本地 / CI 同一命令）。"""
    env = os.environ.copy()
    env["SQLALCHEMY_DATABASE_URL"] = url
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "komari_bot.db.seed_bootstrap"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


async def _recreate_scratch_database() -> dict[str, Any]:
    """重建本文件的一次性隔离库并返回其 asyncpg 连接参数。"""
    base = _parse_dsn(POSTGRES_URL)
    scratch = {**base, "database": f"{base['database']}_tsk197legacy"}
    connection = await asyncpg.connect(**base)
    try:
        name = str(scratch["database"]).replace('"', '""')
        await connection.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await connection.execute(f'CREATE DATABASE "{name}"')
    finally:
        await connection.close()
    return scratch


async def _drop_scratch_database(database: str) -> None:
    """删除一次性隔离库（finally 清理，重复删除安全）。"""
    base = _parse_dsn(POSTGRES_URL)
    connection = await asyncpg.connect(**base)
    try:
        name = database.replace('"', '""')
        await connection.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await connection.close()


async def _insert_legacy_prompt_row(connection: asyncpg.Connection) -> None:
    """在 0011 时代表结构上插入旧聊天 Prompt 单行（含自定义 output）。"""
    await connection.execute(
        f"INSERT INTO {PROMPT_TABLE}"
        " (id, revision, updated_at, system_prompt, memory_ack,"
        "  memory_ack_role, output_instruction, cot_prefix, cot_prefix_role)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
        1,
        3,
        datetime.now(UTC),
        "旧库自定义系统提示词",
        "旧库自定义记忆确认",
        "assistant",
        CUSTOM_OUTPUT_MARKER,
        "旧库自定义思维链前缀",
        "system",
    )


async def _insert_legacy_memory_row(
    connection: asyncpg.Connection,
    *,
    vision_tool_enabled: bool,
) -> None:
    """在 0011 时代表结构上插入 memory 单行（含自定义图片列）。

    0011 时代 memory 表结构与 0014 一致（0012-0014 不触碰 memory 表），
    复用既有迁移验收测试的列推导手法：只显式插入当时已存在的列，图片列
    使用测试自定义值（开关按参数覆盖，预算沿用 ``MEMORY_VISION_VALUES``）。
    """
    exists = await connection.fetchval(
        f"SELECT EXISTS (SELECT 1 FROM {MEMORY_TABLE} WHERE id = 1)"
    )
    assert not exists, "隔离库中不应存在既有 memory 配置行"
    rows = await connection.fetch(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns"
        f" WHERE table_name = '{MEMORY_TABLE}'"
    )
    live_columns = {str(row["column_name"]): row for row in rows}
    assert "vision_tool_enabled" in live_columns, "0011 阶段 memory 应仍持有图片列"
    assert "image_understanding_mode" not in live_columns, "0011 阶段不应有新模式列"

    vision_values = {**MEMORY_VISION_VALUES, "vision_tool_enabled": vision_tool_enabled}
    value_columns: list[str] = []
    values: list[object] = []
    for column in sorted(live_columns):
        if column in ("id", "revision", "updated_at"):
            continue
        value_columns.append(column)
        if column in vision_values:
            values.append(vision_values[column])
        else:
            meta = live_columns[column]
            values.append(
                _memory_row_value(
                    meta["data_type"],
                    nullable=meta["is_nullable"] == "YES",
                )
            )

    columns_sql = ", ".join(["id", "revision", "updated_at", *value_columns])
    placeholders = ", ".join(f"${index}" for index in range(1, 4 + len(value_columns)))
    await connection.execute(
        f"INSERT INTO {MEMORY_TABLE} ({columns_sql}) VALUES ({placeholders})",
        1,
        _MEMORY_LEGACY_REVISION,
        datetime.now(UTC),
        *values,
    )


async def _column_names(connection: asyncpg.Connection, table: str) -> set[str]:
    rows = await connection.fetch(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_name = $1",
        table,
    )
    return {str(row["column_name"]) for row in rows}


def _asset_values(resource_id: str) -> dict[str, str]:
    """从默认 seed 资产读取指定 Prompt 资源的初始值（通用定位，不锁定布局）。"""
    raw = yaml.safe_load(DEFAULT_SEED_FILE.read_text(encoding="utf-8")) or {}
    mapping = find_prompt_mapping(raw, resource_id)
    assert mapping is not None, f"默认 seed 资产缺少 {resource_id} Prompt 初始数据块"
    return {str(field): str(value) for field, value in mapping.items()}


async def _fetch_prompt_row(
    connection: asyncpg.Connection,
    resource_id: str,
) -> dict[str, Any] | None:
    table = prompt_table_name(resource_id)
    row = await connection.fetchrow(f"SELECT * FROM {table} WHERE id = 1")
    return None if row is None else dict(row)


@pytest.mark.skipif(
    not POSTGRES_URL, reason="未设置 KOMARI_TEST_POSTGRES_URL，跳过集成测试"
)
async def test_legacy_database_upgrades_to_head_migrates_values_and_seeds_cleanly() -> None:
    """AC2/AC3：旧配置库一次性升级到 head，值按 spec 迁移且弃用列被删除。

    完整链路：upgrade 0011 → 构造旧 Prompt / 旧图片开关 + 自定义预算 /
    旧 chat 存量行 → upgrade head → 校验列与值迁移 → seed（只补空字段、
    旧自定义 output 不并入任何字段、幂等）→ check 零漂移。
    """
    if not _same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")

    chat_schema_fields = prompt_resource_field_names("komari_chat")
    scratch = await _recreate_scratch_database()
    scratch_url = _scratch_url(str(scratch["database"]))
    try:
        # 停在旧 revision：按 0011 时代表结构构造旧配置库
        result = _run_bootstrap(scratch_url, "upgrade", LEGACY_REVISION)
        assert result.returncode == 0, result.stderr

        connection = await asyncpg.connect(**scratch)
        try:
            await _insert_legacy_prompt_row(connection)
            await _insert_legacy_memory_row(connection, vision_tool_enabled=False)
            await _insert_row_without_budget_columns(
                connection, revision=_CHAT_LEGACY_REVISION
            )
        finally:
            await connection.close()

        # 升级到 head
        result = _run_bootstrap(scratch_url, "upgrade", "head")
        assert result.returncode == 0, result.stderr

        connection = await asyncpg.connect(**scratch)
        try:
            assert await connection.fetchval(
                "SELECT version_num FROM alembic_version"
            ) == HEAD_REVISION, "升级后必须停留在单一 head 0015"

            # 旧聊天 Prompt 列删除 + 新行为列就位
            prompt_columns = await _column_names(connection, PROMPT_TABLE)
            assert "output_instruction" not in prompt_columns, (
                "旧列 output_instruction 未删除"
            )
            for field in sorted(chat_schema_fields):
                assert field in prompt_columns, f"新 Schema 字段 {field} 必须是数据库列"

            # memory 旧图片列全部删除，chat 持有新图片列
            memory_columns = await _column_names(connection, MEMORY_TABLE)
            assert not set(MEMORY_LEGACY_IMAGE_COLUMNS) & memory_columns, (
                "head 的 memory 不得再含 0014 历史图片列"
            )
            assert "image_understanding_mode" not in memory_columns

            # 旧图片开关 false → native；8 项预算原样复制；chat 存量 revision 保留
            chat_image_row = await connection.fetchrow(
                "SELECT image_understanding_mode,"
                " vision_image_download_max_count,"
                " vision_image_download_max_bytes,"
                " vision_image_download_total_max_bytes,"
                " vision_image_download_max_pixels,"
                " vision_image_download_concurrency,"
                " vision_image_download_connect_timeout_seconds,"
                " vision_image_download_read_timeout_seconds,"
                " vision_image_download_total_timeout_seconds"
                f" FROM {CHAT_TABLE} WHERE id = 1"
            )
            assert chat_image_row["image_understanding_mode"] == "native", (
                "vision_tool_enabled=false 应映射 native"
            )
            assert [
                chat_image_row[column] for column in _BUDGET_COLUMNS
            ] == [MEMORY_VISION_VALUES[column] for column in _BUDGET_COLUMNS], (
                "8 项预算应原样从 memory 复制到 chat"
            )
            assert await connection.fetchval(
                f"SELECT revision FROM {CHAT_TABLE} WHERE id = 1"
            ) == _CHAT_LEGACY_REVISION, "chat 存量数据必须保留"

            # 0013/0014 新列以数据库默认值补齐存量行
            chat_defaults_row = await connection.fetchrow(
                "SELECT agent_tool_call_mode, agent_max_rounds,"
                " agent_max_tool_calls_per_round, agent_max_total_tool_calls"
                f" FROM {CHAT_TABLE} WHERE id = 1"
            )
            assert chat_defaults_row["agent_tool_call_mode"] == "required"
            assert (
                chat_defaults_row["agent_max_rounds"],
                chat_defaults_row["agent_max_tool_calls_per_round"],
                chat_defaults_row["agent_max_total_tool_calls"],
            ) == (10, 4, 20)

            # memory 存量 revision 保留
            assert await connection.fetchval(
                f"SELECT revision FROM {MEMORY_TABLE} WHERE id = 1"
            ) == _MEMORY_LEGACY_REVISION, "memory 存量数据必须保留"
        finally:
            await connection.close()

        # 播种：只补新行为字段，绝不覆盖非空自定义值；旧 output 不并入任何字段
        result = _run_seed(scratch_url)
        output = f"{result.stdout}\n{result.stderr}"
        assert result.returncode == 0, output
        assert "新建 2 行" in output, (
            f"chat Prompt 行已存在，seed 应新建 memory/group 两个资源行: {output}"
        )

        connection = await asyncpg.connect(**scratch)
        try:
            row = await _fetch_prompt_row(connection, "komari_chat")
            assert row is not None
            assert row["system_prompt"] == "旧库自定义系统提示词"
            assert row["memory_ack"] == "旧库自定义记忆确认"
            assert row["memory_ack_role"] == "assistant"
            assert row["cot_prefix"] == "旧库自定义思维链前缀"
            assert row["cot_prefix_role"] == "system"

            asset_values = _asset_values("komari_chat")
            merged_new_fields = sorted(
                chat_schema_fields - LEGACY_CHAT_COLUMNS
            )
            for field in merged_new_fields:
                assert row[field] == asset_values[field], (
                    f"迁移后 seed 应补齐新字段 {field}"
                )

            leaked = [
                field
                for field, value in row.items()
                if isinstance(value, str) and CUSTOM_OUTPUT_MARKER in value
            ]
            assert leaked == [], (
                "被删除的自定义 output_instruction 不得并入其他字段: "
                f"{leaked}"
            )

            # 重复播种幂等
            before = dict(row)
            result = _run_seed(scratch_url)
            assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
            after = await _fetch_prompt_row(connection, "komari_chat")
            assert after == before, "重复播种不得改写 chat Prompt 行"
        finally:
            await connection.close()

        # 模型元数据零漂移
        check_result = _run_bootstrap(scratch_url, "check")
        assert check_result.returncode == 0, (
            f"{check_result.stdout}\n{check_result.stderr}"
        )
    finally:
        await _drop_scratch_database(str(scratch["database"]))
