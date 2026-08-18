"""TSK-190 聊天 Prompt 数据库初始数据 —— PostgreSQL 集成验收。

依赖已执行 ``alembic upgrade head`` 的迁移管理 schema
（``KOMARI_TEST_POSTGRES_URL`` 门控，且与 ``SQLALCHEMY_DATABASE_URL``
同库，否则按既有约定 skip）。

隔离纪律：集成用例在从门控 DSN 派生的一次性隔离库（库名后缀
``_promptseed`` / ``_promptmig``）内执行，用例结束即 DROP；共享门控库的
版本与数据不受影响。门控用户需要 CREATEDB 权限。

覆盖清单（对应验收标准）：
- 全新数据库：seed 把聊天 Prompt 完整初始值写入 PostgreSQL，字段与
  强类型 Schema 一致，重复执行幂等（AC1/AC6/AC9）；
- 部分已有：只补空字段、绝不覆盖非空自定义值，重复执行幂等（AC6/AC9）；
- 已完整自定义：seed 完全不动已有非空 Prompt（AC6）；
- 旧库迁移：0011 → head 显式删除 ``output_instruction`` 列并新增行为列，
  被删除的自定义值不被并入任何新字段（AC3/AC9）。

本文件断言不复制生产默认正文：期望值一律从默认 seed 资产经通用定位
（``find_chat_prompt_mapping``）读取，或使用测试自有的字面量。
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
from tests.config.chat_prompt_field_contract import (
    LEGACY_CHAT_COLUMNS,
    find_chat_prompt_mapping,
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
    """在仓库根目录执行 orm_bootstrap 迁移命令（隔离库 URL 环境变量覆盖）。"""
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


def _scratch_url(database: str) -> str:
    """把门控 DSN 的库名替换为隔离库名，其余连接参数保持不变。"""
    return urlparse(POSTGRES_URL)._replace(path=f"/{database}").geturl()


async def _recreate_scratch_database(suffix: str) -> dict[str, Any]:
    """重建一次性隔离库并返回其 asyncpg 连接参数。

    隔离库名 = 门控库名 + 后缀；先 DROP（FORCE 断开残留连接）再 CREATE。
    """
    base = _parse_dsn(POSTGRES_URL)
    scratch = {**base, "database": f"{base['database']}{suffix}"}
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


async def _prepare_head_scratch(
    suffix: str,
) -> tuple[dict[str, Any], str]:
    """重建隔离库并 upgrade head；返回 (scratch 连接参数, scratch URL)。"""
    scratch = await _recreate_scratch_database(suffix)
    scratch_url = _scratch_url(str(scratch["database"]))
    result = _run_bootstrap(scratch_url, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    return scratch, scratch_url


def _chat_asset_values() -> dict[str, str]:
    """从默认 seed 资产读取聊天 Prompt 初始值（通用定位，不锁定布局）。"""
    raw = yaml.safe_load(DEFAULT_SEED_FILE.read_text(encoding="utf-8")) or {}
    mapping = find_chat_prompt_mapping(raw)
    assert mapping is not None, "默认 seed 资产缺少聊天 Prompt 初始数据块"
    return {str(field): str(value) for field, value in mapping.items()}


def _chat_schema_fields() -> set[str]:
    from komari_bot.plugins.komari_chat.prompt_schema import (
        KomariChatPromptSchema,
    )

    return set(KomariChatPromptSchema.model_fields) - {"id", "revision", "updated_at"}


async def _column_names(
    connection: asyncpg.Connection,
    table_name: str,
) -> set[str]:
    rows = await connection.fetch(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = $1
        """,
        table_name,
    )
    return {str(row["column_name"]) for row in rows}


async def _insert_chat_row(
    connection: asyncpg.Connection,
    values: dict[str, str],
    *,
    revision: int = 1,
) -> None:
    """按强类型 Schema 当前列集合插入聊天 Prompt 单行（列集合运行时推导）。"""
    fields = sorted(values)
    columns_sql = ", ".join(["id", "revision", "updated_at", *fields])
    placeholders = ", ".join(f"${index}" for index in range(1, 4 + len(fields)))
    await connection.execute(
        f"INSERT INTO komari_prompt_komari_chat ({columns_sql})"
        f" VALUES ({placeholders})",
        1,
        revision,
        datetime.now(UTC),
        *[values[field] for field in fields],
    )


async def _fetch_chat_row(
    connection: asyncpg.Connection,
) -> dict[str, Any] | None:
    row = await connection.fetchrow(
        "SELECT * FROM komari_prompt_komari_chat WHERE id = 1"
    )
    return None if row is None else dict(row)


async def test_fresh_db_seed_writes_complete_chat_prompt_row_and_is_idempotent() -> None:
    """AC1/AC6/AC9-新库：seed 写入完整聊天 Prompt 初始值；重跑零变化。"""
    if not _same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")

    scratch, scratch_url = await _prepare_head_scratch("_promptseed")
    try:
        result = _run_seed(scratch_url)
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

        connection = await asyncpg.connect(**scratch)
        try:
            row = await _fetch_chat_row(connection)
            assert row is not None, "seed 后聊天 Prompt 单行必须存在"
            schema_fields = _chat_schema_fields()
            assert set(row) - {"id", "revision", "updated_at"} == schema_fields

            asset_values = _chat_asset_values()
            for field in sorted(schema_fields):
                assert bool(str(row[field]).strip()), (
                    f"聊天 Prompt 字段 {field} 初始值不得为空"
                )
                assert row[field] == asset_values[field], (
                    f"聊天 Prompt 字段 {field} 必须等于 seed 资产值"
                )
            assert row["revision"] == 1

            result = _run_seed(scratch_url)
            assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
            row_after = await _fetch_chat_row(connection)
            assert row_after == row, "重复播种不得产生 revision/content 变化"
        finally:
            await connection.close()
    finally:
        await _drop_scratch_database(str(scratch["database"]))


async def test_seed_fills_empty_fields_and_keeps_custom_values() -> None:
    """AC6/AC9：只补空字段；非空自定义值原样保留；重跑幂等。"""
    if not _same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")

    scratch, scratch_url = await _prepare_head_scratch("_promptseed")
    try:
        connection = await asyncpg.connect(**scratch)
        try:
            fields = sorted(_chat_schema_fields())
            custom_fields = {"system_prompt", "tool_call_instruction"}
            values = {
                field: (f"custom-{field}" if field in custom_fields else "")
                for field in fields
            }
            await _insert_chat_row(connection, values)

            result = _run_seed(scratch_url)
            assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

            row = await _fetch_chat_row(connection)
            assert row is not None
            asset_values = _chat_asset_values()
            for field in custom_fields:
                assert row[field] == f"custom-{field}", (
                    f"seed 不得覆盖非空自定义字段 {field}"
                )
            empty_before = [field for field in fields if field not in custom_fields]
            for field in empty_before:
                assert row[field] == asset_values[field], (
                    f"seed 应补齐空字段 {field}"
                )
            assert row["revision"] >= 1

            before = await _fetch_chat_row(connection)
            result = _run_seed(scratch_url)
            assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
            after = await _fetch_chat_row(connection)
            assert before == after, "补齐后重复播种不得产生任何变化"
        finally:
            await connection.close()
    finally:
        await _drop_scratch_database(str(scratch["database"]))


async def test_seed_never_touches_fully_custom_row() -> None:
    """AC6：全部字段已有非空自定义值时，seed 一次也不写入。"""
    if not _same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")

    scratch, scratch_url = await _prepare_head_scratch("_promptseed")
    try:
        connection = await asyncpg.connect(**scratch)
        try:
            fields = sorted(_chat_schema_fields())
            values = {field: f"custom-{field}" for field in fields}
            await _insert_chat_row(connection, values, revision=2)

            before = await _fetch_chat_row(connection)
            result = _run_seed(scratch_url)
            assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
            after_first = await _fetch_chat_row(connection)
            assert after_first == before, (
                "首轮播种不得改写任何非空自定义字段或 revision"
            )

            result = _run_seed(scratch_url)
            assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
            after_second = await _fetch_chat_row(connection)
            assert after_second == before, "重复播种仍不得改写自定义行"
        finally:
            await connection.close()
    finally:
        await _drop_scratch_database(str(scratch["database"]))


async def test_old_db_migration_drops_output_instruction_and_keeps_custom_fields() -> None:
    """AC3/AC9-旧库：0011 → head 删除旧列、新增行为列，自定义 output 不并入。

    旧库从迁移链的旧 revision（0011）构造；不修改历史 migration。
    被删除的自定义 ``output_instruction`` 值必须消失且不并入任何新字段。
    """
    if not _same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")

    scratch = await _recreate_scratch_database("_promptmig")
    scratch_url = _scratch_url(str(scratch["database"]))
    result = _run_bootstrap(scratch_url, "upgrade", "0011")
    assert result.returncode == 0, result.stderr

    connection = await asyncpg.connect(**scratch)
    try:
        custom_marker = "CUSTOM-OUTPUT-MARKER-tsk190"
        await connection.execute(
            "INSERT INTO komari_prompt_komari_chat"
            " (id, revision, updated_at, system_prompt, memory_ack,"
            "  memory_ack_role, output_instruction, cot_prefix,"
            "  cot_prefix_role)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
            1,
            3,
            datetime.now(UTC),
            "旧库自定义系统提示词",
            "旧库自定义记忆确认",
            "assistant",
            custom_marker,
            "旧库自定义思维链前缀",
            "system",
        )
        old_columns = await _column_names(connection, "komari_prompt_komari_chat")
        assert old_columns >= LEGACY_CHAT_COLUMNS, "0011 版本应具备旧 Prompt 列"

        result = _run_bootstrap(scratch_url, "upgrade", "head")
        assert result.returncode == 0, result.stderr
        assert (
            await connection.fetchval("SELECT version_num FROM alembic_version")
            != "0011"
        ), "迁移必须离开旧 revision"

        columns = await _column_names(connection, "komari_prompt_komari_chat")
        assert "output_instruction" not in columns, "旧列 output_instruction 未删除"
        schema_fields = _chat_schema_fields()
        for field in sorted(schema_fields):
            assert field in columns, f"新 Schema 字段 {field} 必须是数据库列"

        # 迁移后播种：非 output 旧自定义值保留，新列由 seed 补齐
        result = _run_seed(scratch_url)
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

        row = await _fetch_chat_row(connection)
        assert row is not None
        assert row["system_prompt"] == "旧库自定义系统提示词"
        assert row["memory_ack"] == "旧库自定义记忆确认"
        assert row["memory_ack_role"] == "assistant"
        assert row["cot_prefix"] == "旧库自定义思维链前缀"
        assert row["cot_prefix_role"] == "system"

        asset_values = _chat_asset_values()
        merged_new_fields = sorted(schema_fields - LEGACY_CHAT_COLUMNS)
        for field in merged_new_fields:
            assert row[field] == asset_values[field], (
                f"迁移后 seed 应补齐新字段 {field}"
            )

        leaked = [
            field
            for field, value in row.items()
            if isinstance(value, str) and custom_marker in value
        ]
        assert leaked == [], (
            "被删除的自定义 output_instruction 不得并入其他字段: "
            f"{leaked}"
        )
    finally:
        await connection.close()
        await _drop_scratch_database(str(scratch["database"]))
