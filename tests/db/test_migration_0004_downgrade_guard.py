"""迁移 0004 downgrade 空行防护验收测试（KOMARIBOT-13）。

静态守卫沿用迁移链测试的文本校验手法；集成测试以
``KOMARI_TEST_POSTGRES_URL`` 门控，并要求 ``SQLALCHEMY_DATABASE_URL``
与门控 DSN 同库，否则跳过（与 0010/0011 迁移验收同一守卫手法）。

隔离纪律：0011 起迁移链不可逆，链驱动验收不得搬移共享门控库的
版本。集成流程在从门控 DSN 派生的一次性隔离库（库名后缀
``_mig0004``）内重建迁移链——先 upgrade 0004，再演练回滚 0003
与升级恢复，用例结束即 DROP；共享门控库始终保持 head，重复执行
与执行顺序互不影响。门控用户需要 CREATEDB 权限。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import asyncpg
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = PROJECT_ROOT / "migrations" / "versions" / "0004_komari_chat_config.py"

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")
SQLALCHEMY_URL = os.getenv("SQLALCHEMY_DATABASE_URL", "")

#: 迁移 0002/0004 之间 komari_memory_config 被 DROP 的 11 列。
_DROPPED_COLUMNS = (
    "proactive_enabled",
    "proactive_score_threshold",
    "proactive_cooldown",
    "proactive_max_per_hour",
    "proactive_reservation_ttl_seconds",
    "reply_commit_worker_interval_seconds",
    "reply_commit_batch_size",
    "reply_commit_lease_seconds",
    "reply_commit_max_attempts",
    "reply_commit_retry_base_seconds",
    "reply_commit_tombstone_retention_days",
)

#: 死字段（KOMARIBOT-7 已从 schema 删除），downgrade 回填的默认值。
_DEAD_FIELD_DEFAULT = 0.0

#: 0004 时代 komari_memory_config 仍持有、但 head schema（TSK-194 于
#: 0015 迁入 komari_chat_config）已移除的 9 个视觉字段，均为 NOT NULL
#: 且无默认值。历史 fixture 在 0004 时代表结构插入单行时必须显式补齐，
#: 否则 INSERT 即 NOT NULL 违约。
_HISTORICAL_MEMORY_VISION_DEFAULTS: dict[str, object] = {
    "vision_tool_enabled": True,
    "vision_image_download_max_count": 4,
    "vision_image_download_max_bytes": 8 * 1024 * 1024,
    "vision_image_download_total_max_bytes": 20 * 1024 * 1024,
    "vision_image_download_max_pixels": 40_000_000,
    "vision_image_download_concurrency": 2,
    "vision_image_download_connect_timeout_seconds": 5.0,
    "vision_image_download_read_timeout_seconds": 30.0,
    "vision_image_download_total_timeout_seconds": 45.0,
}

_RENAMED_CONFIG_COLUMNS = {
    "reply_commit_worker_interval_seconds": (
        "reply_fulfillment_worker_interval_seconds"
    ),
    "reply_commit_batch_size": "reply_fulfillment_batch_size",
    "reply_commit_lease_seconds": "reply_fulfillment_lease_seconds",
    "reply_commit_max_attempts": "reply_fulfillment_max_attempts",
    "reply_commit_retry_base_seconds": "reply_fulfillment_retry_base_seconds",
    "reply_commit_tombstone_retention_days": (
        "reply_fulfillment_tombstone_retention_days"
    ),
}


def _migration_downgrade_source() -> str:
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    start = source.index("def downgrade(")
    return source[start:]


def test_downgrade_backfill_is_guarded_against_missing_chat_config_row() -> None:
    """downgrade 回填对 komari_chat_config 空行场景显式防护。

    无行时子查询返回 NULL 行，直接 ``SET (cols) = (...)`` 会给 NOT NULL
    列赋 NULL 而抛错；回填语句必须携带 EXISTS 守卫或 COALESCE 回落。
    """
    downgrade_body = _migration_downgrade_source()
    assert re.search(r"\bEXISTS\b|\bCOALESCE\b", downgrade_body, re.IGNORECASE), (
        "downgrade 回填缺少空行防护（EXISTS 守卫或 COALESCE 回落）"
    )


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
    """在仓库根目录执行 orm_bootstrap 迁移命令。

    子进程 cwd 必须是仓库根：nonebot-plugin-orm 按 cwd 相对的
    ``migrations/`` 定位版本链，nonebot 配置（含 ``.env`` 覆盖层）
    也在该目录加载。``SQLALCHEMY_DATABASE_URL`` 经环境变量显式覆盖
    为隔离库 URL，优先级高于 dotenv 文件。
    """
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

    隔离库名 = 门控库名 + ``_mig0004``；先 DROP（FORCE 断开残留
    连接）再 CREATE，重复执行幂等。门控用户需要 CREATEDB 权限。
    """
    base = _parse_dsn(POSTGRES_URL)
    scratch = {**base, "database": f"{base['database']}_mig0004"}
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


def _expected_schema_defaults() -> dict[str, object]:
    """10 个活字段的回填期望值 = komari_chat schema 默认值。"""
    from komari_bot.plugins.komari_chat.config_schema import (
        KomariChatConfigSchema,
    )

    defaults = KomariChatConfigSchema()
    values: dict[str, object] = {
        column: getattr(defaults, _RENAMED_CONFIG_COLUMNS.get(column, column))
        for column in _DROPPED_COLUMNS
        if column != "proactive_score_threshold"
    }
    values["proactive_score_threshold"] = _DEAD_FIELD_DEFAULT
    return values


@pytest.mark.skipif(
    not POSTGRES_URL, reason="未设置 KOMARI_TEST_POSTGRES_URL，跳过集成测试"
)
async def test_downgrade_empty_row_and_populated_row_scenarios() -> None:
    """空行与有行两种场景的 downgrade 行为验证。

    1. 空行：upgrade 0004 后 komari_chat_config 尚未初始化（无行），
       downgrade 不抛错，komari_memory_config 按 schema 默认值回填；
    2. 有行：活字段值原样回填，11 列结构复原；
    3. 全程在一次性隔离库内执行，用例结束即删除，共享门控库的
       版本不受搬移影响。
    """
    if not _same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")

    scratch = await _recreate_scratch_database()
    scratch_url = _scratch_url(str(scratch["database"]))
    result = _run_bootstrap(scratch_url, "upgrade", "0004")
    assert result.returncode == 0, result.stderr

    conn = await asyncpg.connect(**scratch)
    try:
        await _ensure_memory_config_row(conn)

        # === 场景一：komari_chat_config 无行 ===
        await conn.execute("DELETE FROM komari_chat_config WHERE id = 1")
        result = _run_bootstrap(scratch_url, "downgrade", "0003")
        assert result.returncode == 0, f"空行场景 downgrade 失败: {result.stderr}"

        chat_table_exists = await conn.fetchval(
            "SELECT to_regclass('komari_chat_config') IS NOT NULL"
        )
        assert not chat_table_exists, "downgrade 后 komari_chat_config 应已删除"

        memory_columns = await _memory_config_columns(conn)
        assert set(_DROPPED_COLUMNS) <= set(memory_columns), "11 列结构未复原"

        memory_row = await conn.fetchrow(
            "SELECT * FROM komari_memory_config WHERE id = 1"
        )
        assert memory_row is not None, "komari_memory_config 单行应存在"
        expected_defaults = _expected_schema_defaults()
        for column, expected in expected_defaults.items():
            assert memory_row[column] == expected, (
                f"空行回填默认值不一致: {column}={memory_row[column]!r} "
                f"expected={expected!r}"
            )

        # === 场景二：komari_chat_config 有行，活字段原样回填 ===
        result = _run_bootstrap(scratch_url, "upgrade", "0004")
        assert result.returncode == 0, result.stderr

        # 隔离库无运行时播种，先按 schema 默认值补插单行再写特色值
        await _ensure_chat_config_row(conn)
        distinctive = dict(_expected_schema_defaults())
        distinctive.pop("proactive_score_threshold")
        distinctive["proactive_cooldown"] = 123
        distinctive["reply_commit_batch_size"] = 7
        set_clause = ", ".join(
            f"{column} = ${index}" for index, column in enumerate(distinctive, start=1)
        )
        await conn.execute(
            f"UPDATE komari_chat_config SET {set_clause} WHERE id = 1",
            *distinctive.values(),
        )

        result = _run_bootstrap(scratch_url, "downgrade", "0003")
        assert result.returncode == 0, f"有行场景 downgrade 失败: {result.stderr}"

        memory_row = await conn.fetchrow(
            "SELECT * FROM komari_memory_config WHERE id = 1"
        )
        assert memory_row is not None
        assert memory_row["proactive_cooldown"] == 123
        assert memory_row["reply_commit_batch_size"] == 7
        for column in _DROPPED_COLUMNS:
            if column in (
                "proactive_cooldown",
                "reply_commit_batch_size",
                "proactive_score_threshold",
            ):
                continue
            expected = _expected_schema_defaults()[column]
            assert memory_row[column] == expected, column

        memory_columns = await _memory_config_columns(conn)
        assert set(_DROPPED_COLUMNS) <= set(memory_columns), "有行场景 11 列结构未复原"
    finally:
        await conn.close()
        await _drop_scratch_database(str(scratch["database"]))


async def _ensure_chat_config_row(conn: asyncpg.Connection) -> None:
    """0004 后隔离库无运行时播种，按 schema 默认值补插 chat 配置单行。

    0004 版本下 ``komari_chat_config`` 的值列恰好是 10 个旧名活字段，
    直接复用 ``_expected_schema_defaults()`` 的旧名 → 默认值映射。
    """
    exists = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM komari_chat_config WHERE id = 1)"
    )
    if exists:
        return
    defaults = _expected_schema_defaults()
    columns = list(defaults)
    columns_sql = ", ".join(["id", "revision", "updated_at", *columns])
    placeholders = ", ".join(f"${index}" for index in range(1, 4 + len(columns)))
    await conn.execute(
        f"INSERT INTO komari_chat_config ({columns_sql}) VALUES ({placeholders})",
        1,
        1,
        datetime.now(UTC),
        *defaults.values(),
    )


async def _ensure_memory_config_row(conn: asyncpg.Connection) -> bool:
    """确保 komari_memory_config 单行存在（无行时按 schema 默认插入）。

    Returns:
        是否由本函数新建了该行。
    """
    exists = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM komari_memory_config WHERE id = 1)"
    )
    if exists:
        return False

    from komari_bot.plugins.komari_memory.config_schema import (
        KomariMemoryConfigSchema,
    )

    defaults = KomariMemoryConfigSchema()
    table_columns = await _memory_config_columns(conn)
    value_columns = [
        column
        for column in table_columns
        if column not in ("id", "revision", "updated_at")
        and column in type(defaults).model_fields
    ]
    values = [getattr(defaults, column) for column in value_columns]
    # TSK-194：0015 迁走前 memory 表仍持有 9 个视觉列（NOT NULL 无默认），
    # head schema 已无对应字段；历史 fixture 须显式补齐，避免 NOT NULL 违约。
    for column, value in _HISTORICAL_MEMORY_VISION_DEFAULTS.items():
        if column in table_columns and column not in value_columns:
            value_columns.append(column)
            values.append(value)
    # JSONB 列（白名单等列表/字典字段）必须序列化并显式 ::jsonb 转型，
    # 否则 asyncpg 把 list 当数组绑定而报 DataError
    serialized = [
        json.dumps(value, ensure_ascii=False)
        if isinstance(value, (dict, list))
        else value
        for value in values
    ]
    placeholders = ", ".join(
        f"${index}::jsonb"
        if isinstance(values[index - 4], (dict, list))
        else f"${index}"
        for index in range(4, 4 + len(value_columns))
    )
    columns_sql = ", ".join(["id", "revision", "updated_at", *value_columns])
    await conn.execute(
        f"INSERT INTO komari_memory_config ({columns_sql})"
        f" VALUES ($1, $2, $3, {placeholders})",
        1,
        1,
        datetime.now(UTC),
        *serialized,
    )
    return True


async def _memory_config_columns(conn: asyncpg.Connection) -> list[str]:
    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_name = 'komari_memory_config' ORDER BY ordinal_position"
    )
    return [row["column_name"] for row in rows]
