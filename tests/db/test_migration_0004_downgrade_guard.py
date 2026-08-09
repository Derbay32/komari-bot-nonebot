"""迁移 0004 downgrade 空行防护验收测试（KOMARIBOT-13）。

静态守卫沿用迁移链测试的文本校验手法；集成测试以
``KOMARI_TEST_POSTGRES_URL`` 门控，与 nonebot 配置的
``sqlalchemy_database_url`` 不同库时跳过（沿用既有守卫手法）。

集成流程会把测试库临时回滚到 0003 再升级回 head，``finally`` 中
始终执行 ``upgrade head`` 恢复迁移状态，并把 ``komari_chat_config``
单行数据还原为测试前快照。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

import asyncpg
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = (
    PROJECT_ROOT / "migrations" / "versions" / "0004_komari_chat_config.py"
)

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")

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
    assert re.search(
        r"\bEXISTS\b|\bCOALESCE\b", downgrade_body, re.IGNORECASE
    ), "downgrade 回填缺少空行防护（EXISTS 守卫或 COALESCE 回落）"


def _configured_database_url() -> str:
    from nonebot import get_driver

    return str(getattr(get_driver().config, "sqlalchemy_database_url", "") or "")


def _same_database(left: str, right: str) -> bool:
    left_parsed = urlparse(left)
    right_parsed = urlparse(right)
    return (
        left_parsed.hostname == right_parsed.hostname
        and (left_parsed.port or 5432) == (right_parsed.port or 5432)
        and left_parsed.path == right_parsed.path
    )


def _parse_dsn(url: str) -> dict[str, object]:
    parsed = urlparse(url.replace("postgresql+asyncpg://", "postgresql://"))
    return {
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "database": parsed.path.lstrip("/"),
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
    }


def _run_bootstrap(*args: str) -> subprocess.CompletedProcess[str]:
    """在仓库根目录执行 orm_bootstrap 迁移命令。

    子进程 cwd 必须是仓库根：nonebot-plugin-orm 按 cwd 相对的
    ``migrations/`` 定位版本链，nonebot 配置（含 ``.env`` 覆盖层）
    也在该目录加载。``SQLALCHEMY_DATABASE_URL`` 经环境变量显式覆盖，
    优先级高于 dotenv 文件。
    """
    env = os.environ.copy()
    env["SQLALCHEMY_DATABASE_URL"] = POSTGRES_URL
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


def _expected_schema_defaults() -> dict[str, object]:
    """10 个活字段的回填期望值 = komari_chat schema 默认值。"""
    from komari_bot.plugins.komari_chat.config_schema import (
        KomariChatConfigSchema,
    )

    defaults = KomariChatConfigSchema()
    values: dict[str, object] = {
        column: getattr(defaults, column) for column in _DROPPED_COLUMNS
        if column != "proactive_score_threshold"
    }
    values["proactive_score_threshold"] = _DEAD_FIELD_DEFAULT
    return values


@pytest.mark.skipif(
    not POSTGRES_URL, reason="未设置 KOMARI_TEST_POSTGRES_URL，跳过集成测试"
)
async def test_downgrade_empty_row_and_populated_row_scenarios() -> None:
    """空行与有行两种场景的 downgrade 行为验证。

    1. 空行：upgrade head 后 komari_chat_config 尚未初始化（无行），
       downgrade 不抛错，komari_memory_config 按 schema 默认值回填；
    2. 有行：活字段值原样回填，11 列结构复原；
    3. 最终 upgrade head 恢复迁移状态，并还原配置数据快照。
    """
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")

    result = _run_bootstrap("upgrade", "head")
    assert result.returncode == 0, result.stderr

    conn = await asyncpg.connect(**_parse_dsn(POSTGRES_URL))
    original_chat_row: asyncpg.Record | None = None
    created_memory_row = False
    try:
        original_chat_row = await conn.fetchrow(
            "SELECT * FROM komari_chat_config WHERE id = 1"
        )
        created_memory_row = await _ensure_memory_config_row(conn)

        # === 场景一：komari_chat_config 无行 ===
        await conn.execute("DELETE FROM komari_chat_config WHERE id = 1")
        result = _run_bootstrap("downgrade", "0003")
        assert result.returncode == 0, (
            f"空行场景 downgrade 失败: {result.stderr}"
        )

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
        result = _run_bootstrap("upgrade", "head")
        assert result.returncode == 0, result.stderr

        distinctive = dict(_expected_schema_defaults())
        distinctive.pop("proactive_score_threshold")
        distinctive["proactive_cooldown"] = 123
        distinctive["reply_commit_batch_size"] = 7
        set_clause = ", ".join(f"{column} = ${index}" for index, column in enumerate(distinctive, start=1))
        await conn.execute(
            f"UPDATE komari_chat_config SET {set_clause} WHERE id = 1",
            *distinctive.values(),
        )

        result = _run_bootstrap("downgrade", "0003")
        assert result.returncode == 0, (
            f"有行场景 downgrade 失败: {result.stderr}"
        )

        memory_row = await conn.fetchrow(
            "SELECT * FROM komari_memory_config WHERE id = 1"
        )
        assert memory_row is not None
        assert memory_row["proactive_cooldown"] == 123
        assert memory_row["reply_commit_batch_size"] == 7
        for column in _DROPPED_COLUMNS:
            if column in ("proactive_cooldown", "reply_commit_batch_size", "proactive_score_threshold"):
                continue
            expected = _expected_schema_defaults()[column]
            assert memory_row[column] == expected, column

        memory_columns = await _memory_config_columns(conn)
        assert set(_DROPPED_COLUMNS) <= set(memory_columns), "有行场景 11 列结构未复原"
    finally:
        # === 恢复：迁移回到 head，数据还原为测试前快照 ===
        result = _run_bootstrap("upgrade", "head")
        assert result.returncode == 0, result.stderr
        if original_chat_row is not None:
            chat_columns = [
                column for column in dict(original_chat_row) if column != "updated_at"
            ]
            set_clause = ", ".join(
                f"{column} = ${index}" for index, column in enumerate(chat_columns, start=1)
            )
            await conn.execute(
                f"UPDATE komari_chat_config SET {set_clause} WHERE id = 1",
                *[original_chat_row[column] for column in chat_columns],
            )
        else:
            await conn.execute("DELETE FROM komari_chat_config WHERE id = 1")
        if created_memory_row:
            await conn.execute("DELETE FROM komari_memory_config WHERE id = 1")
        await conn.close()


async def _ensure_memory_config_row(conn: asyncpg.Connection) -> bool:
    """确保 komari_memory_config 单行存在（无行时按 schema 默认插入）。

    Returns:
        是否由本函数新建了该行（供 finally 决定是否删除以恢复原状）。
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
    columns_sql = ", ".join(["id", "revision", "updated_at", *value_columns])
    placeholders = ", ".join(
        f"${index}" for index in range(1, 4 + len(value_columns))
    )
    await conn.execute(
        f"INSERT INTO komari_memory_config ({columns_sql}) VALUES ({placeholders})",
        1,
        1,
        datetime.now(UTC),
        *[getattr(defaults, column) for column in value_columns],
    )
    return True


async def _memory_config_columns(conn: asyncpg.Connection) -> list[str]:
    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_name = 'komari_memory_config' ORDER BY ordinal_position"
    )
    return [row["column_name"] for row in rows]
