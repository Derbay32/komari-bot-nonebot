"""TSK-194 图片理解模式与下载预算跨表迁移的 PostgreSQL 验收。

隔离纪律：用例在从门控 DSN 派生的一次性隔离库（库名后缀
``_vision0015``）内重建迁移链，验证 0015 的完整语义：

1. **跨表迁移**：0014 时代表结构上写入 memory 单行（vision_tool_enabled
   切换 + 8 项预算自定义值）与 chat 单行（无图片列）；``upgrade head``
   后 chat 以 bool 双分支映射出 ``image_understanding_mode``（true →
   delegated）并原样复制 8 项预算，memory 的 9 个旧图片列被显式删除，
   两表存量行数据全部保留；
2. **降级回滚**：``downgrade 0014`` 重建 memory 旧列、按模式回填
   （delegated → true）与预算原样回填、删除 chat 新列，存量行保留；
3. **全新库默认与约束**：head 下只让图片列走数据库默认的一条 chat 行
   得到 ``delegated`` 与 8 项默认预算；两句图片预算跨字段 CHECK 拒绝
   非法组合（总字节 < 单图字节、总时限 < 连接超时）；
4. 回到 head 后 ``orm_bootstrap check`` 验证模型元数据零漂移。
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
from asyncpg.exceptions import CheckViolationError

from tests.db.test_agent_budget_config_migration import (
    _NON_BUDGET_COLUMN_DEFAULTS,
    _insert_row_without_budget_columns,
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

_MEMORY_TABLE = "komari_memory_config"
_CHAT_TABLE = "komari_chat_config"

#: 8 项图片下载预算列，chat head 与 memory 0014 历史两集合共有的部分。
_BUDGET_COLUMNS: tuple[str, ...] = (
    "vision_image_download_max_count",
    "vision_image_download_max_bytes",
    "vision_image_download_total_max_bytes",
    "vision_image_download_max_pixels",
    "vision_image_download_concurrency",
    "vision_image_download_connect_timeout_seconds",
    "vision_image_download_read_timeout_seconds",
    "vision_image_download_total_timeout_seconds",
)

#: chat head 图片列集合：image_understanding_mode + 8 项预算（0015 新增）。
CHAT_IMAGE_COLUMNS: tuple[str, ...] = ("image_understanding_mode", *_BUDGET_COLUMNS)

#: memory 0014 历史图片列集合：vision_tool_enabled + 8 项预算（0002 建列，
#: 0015 从 memory 删除；downgrade 0014 必须重建这 9 列）。
MEMORY_LEGACY_IMAGE_COLUMNS: tuple[str, ...] = (
    "vision_tool_enabled",
    *_BUDGET_COLUMNS,
)

#: head 下 chat 图片列的服务端默认值（不显式赋值时的值）。
CHAT_IMAGE_DEFAULT_VALUES: dict[str, object] = {
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

#: 0014 时代 memory 旧图片列的自定义值（迁移后应原样落到 chat；
#: 预算组合满足 chat 的跨字段 CHECK：总字节 >= 单图字节、总时限 >=
#: 连接超时）。
MEMORY_VISION_VALUES: dict[str, object] = {
    "vision_tool_enabled": True,
    "vision_image_download_max_count": 6,
    "vision_image_download_max_bytes": 9 * 1024 * 1024,
    "vision_image_download_total_max_bytes": 21 * 1024 * 1024,
    "vision_image_download_max_pixels": 41_000_000,
    "vision_image_download_concurrency": 3,
    "vision_image_download_connect_timeout_seconds": 6.0,
    "vision_image_download_read_timeout_seconds": 31.0,
    "vision_image_download_total_timeout_seconds": 46.0,
}

#: 两套列集合与其值字典结构自洽，防止后续改动时列集漂移。
assert set(MEMORY_LEGACY_IMAGE_COLUMNS) == set(MEMORY_VISION_VALUES)
assert set(CHAT_IMAGE_COLUMNS) == set(CHAT_IMAGE_DEFAULT_VALUES)

#: 0014 时代 memory 单行使用的 revision（区别于 chat 的 revision，验证
#: 存量数据不被迁移触碰）。
_MEMORY_LEGACY_REVISION = 77
_CHAT_LEGACY_REVISION = 42


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

    隔离库名 = 门控库名 + ``_vision0015``；先 DROP（FORCE 断开残留
    连接）再 CREATE，重复执行幂等。
    """
    base = _parse_dsn(POSTGRES_URL)
    scratch = {**base, "database": f"{base['database']}_vision0015"}
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


def _memory_row_value(data_type: str, *, nullable: bool) -> object:
    """为 memory 存量行的非图片列生成合法的最小占位值。

    0014 时代 memory 表除图片列外均为无默认 NOT NULL（JSONB / BOOLEAN /
    INTEGER / FLOAT / VARCHAR），这里按类型生成占位值；可空列返回
    None。图片列在调用方显式给出，不经过本函数。
    """
    if nullable:
        return None
    if data_type == "boolean":
        return False
    if data_type == "jsonb":
        return "{}"
    if data_type in ("integer", "bigint"):
        return 1
    if data_type in ("real", "double precision"):
        return 1.0
    return ""


async def _insert_legacy_memory_row(connection: asyncpg.Connection) -> None:
    """在 0014 时代表结构上插入 memory 单行（含自定义图片列）。

    只显式插入当时（0014）已存在的列；图片列使用
    ``MEMORY_VISION_VALUES``，其余列按类型生成占位值。
    """
    exists = await connection.fetchval(
        f"SELECT EXISTS (SELECT 1 FROM {_MEMORY_TABLE} WHERE id = 1)"
    )
    assert not exists, "隔离库中不应存在既有 memory 配置行"
    rows = await connection.fetch(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns"
        f" WHERE table_name = '{_MEMORY_TABLE}'"
    )
    live_columns = {
        str(row["column_name"]): row for row in rows
    }
    assert "vision_tool_enabled" in live_columns, "0014 阶段 memory 应仍持有图片列"
    assert "image_understanding_mode" not in live_columns, "0014 阶段不应有新模式列"

    value_columns: list[str] = []
    values: list[object] = []
    for column in sorted(live_columns):
        if column in ("id", "revision", "updated_at"):
            continue
        value_columns.append(column)
        if column in MEMORY_VISION_VALUES:
            values.append(MEMORY_VISION_VALUES[column])
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
        f"INSERT INTO {_MEMORY_TABLE} ({columns_sql}) VALUES ({placeholders})",
        1,
        _MEMORY_LEGACY_REVISION,
        datetime.now(UTC),
        *values,
    )


def _reread_columns(rows: list[asyncpg.Record]) -> set[str]:
    return {str(row["column_name"]) for row in rows}


async def _chat_image_columns(connection: asyncpg.Connection) -> set[str]:
    rows = await connection.fetch(
        "SELECT column_name FROM information_schema.columns"
        f" WHERE table_name = '{_CHAT_TABLE}'"
    )
    return _reread_columns(rows)


async def _memory_columns(connection: asyncpg.Connection) -> set[str]:
    rows = await connection.fetch(
        "SELECT column_name FROM information_schema.columns"
        f" WHERE table_name = '{_MEMORY_TABLE}'"
    )
    return _reread_columns(rows)


async def _fetched_image_row(connection: asyncpg.Connection) -> tuple[object, ...]:
    return await connection.fetchrow(  # type: ignore[return-value]
        "SELECT image_understanding_mode,"
        " vision_image_download_max_count,"
        " vision_image_download_max_bytes,"
        " vision_image_download_total_max_bytes,"
        " vision_image_download_max_pixels,"
        " vision_image_download_concurrency,"
        " vision_image_download_connect_timeout_seconds,"
        " vision_image_download_read_timeout_seconds,"
        " vision_image_download_total_timeout_seconds"
        f" FROM {_CHAT_TABLE} WHERE id = 1"
    )


async def _insert_chat_row_leaving_image_defaults(
    connection: asyncpg.Connection,
) -> None:
    """head 下插入 chat 行，显式提供全部非图片列，让图片列走 DB 默认。

    预算列（0013，非空默认）也一并显式给出，使本行只依赖图片列的新
    默认值，隔离 0015 新增的列行为。
    """
    values: dict[str, object] = {
        **_NON_BUDGET_COLUMN_DEFAULTS,
        "agent_max_rounds": 5,
        "agent_max_tool_calls_per_round": 2,
        "agent_max_total_tool_calls": 8,
    }
    rows = await connection.fetch(
        "SELECT column_name FROM information_schema.columns"
        f" WHERE table_name = '{_CHAT_TABLE}'"
    )
    columns = _reread_columns(rows)
    value_columns = sorted(columns - {"id", "revision", "updated_at"} - set(CHAT_IMAGE_COLUMNS))
    missing = [column for column in value_columns if column not in values]
    assert not missing, f"测试默认值字典缺少列: {missing}"

    columns_sql = ", ".join(["id", "revision", "updated_at", *value_columns])
    placeholders = ", ".join(f"${index}" for index in range(1, 4 + len(value_columns)))
    await connection.execute(
        f"INSERT INTO {_CHAT_TABLE} ({columns_sql}) VALUES ({placeholders})",
        1,
        1,
        datetime.now(UTC),
        *[values[column] for column in value_columns],
    )


@pytest.mark.skipif(
    not POSTGRES_URL, reason="未设置 KOMARI_TEST_POSTGRES_URL，跳过迁移集成测试"
)
async def test_image_columns_migrate_memory_to_chat_and_downgrade_cleanly() -> None:
    """AC1/AC2：跨表迁移、bool 双分支、旧列删除与降级回滚。

    完整链路：upgrade 0014 → 写入 memory（自定义图片列）与 chat 存量行
    → upgrade head（图片列迁入 chat、memory 删除 9 列）→ downgrade 0014
    （重建 memory 旧列并回填、chat 删除新列）→ 回到 head 后 check 零漂移。
    """
    if not _same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")

    scratch = await _recreate_scratch_database()
    scratch_url = _scratch_url(str(scratch["database"]))
    try:
        # 先停在 0014：按 0014 时代表结构写入 memory 与 chat 存量行
        result = _run_bootstrap(scratch_url, "upgrade", "0014")
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        conn = await asyncpg.connect(**scratch)
        try:
            await _insert_legacy_memory_row(conn)
            # 0014 阶段 chat 存量行携带历史 revision，验证 0015 不得触碰
            await _insert_row_without_budget_columns(
                conn, revision=_CHAT_LEGACY_REVISION
            )
        finally:
            await conn.close()

        # upgrade head：图片列迁入 chat、memory 旧列删除
        result = _run_bootstrap(scratch_url, "upgrade", "head")
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        conn = await asyncpg.connect(**scratch)
        try:
            chat_columns = await _chat_image_columns(conn)
            assert set(CHAT_IMAGE_COLUMNS) <= chat_columns, "head 的 chat 必须含图片列"

            memory_columns = await _memory_columns(conn)
            assert not set(MEMORY_LEGACY_IMAGE_COLUMNS) & memory_columns, (
                "head 的 memory 不得再含 0014 历史图片列"
            )
            assert "image_understanding_mode" not in memory_columns

            # bool 双分支 true → delegated；8 项预算原样复制
            row = await _fetched_image_row(conn)
            chat_revision = await conn.fetchval(
                f"SELECT revision FROM {_CHAT_TABLE} WHERE id = 1"
            )
            assert row[0] == "delegated", "vision_tool_enabled=true 应映射 delegated"
            expected = tuple(
                MEMORY_VISION_VALUES[column]
                for column in _BUDGET_COLUMNS
            )
            assert row[1:] == expected, "8 项预算应原样从 memory 复制到 chat"
            assert chat_revision == _CHAT_LEGACY_REVISION, "chat 存量数据必须保留"

            # memory 存量行（非图片列）保留
            memory_revision = await conn.fetchval(
                f"SELECT revision FROM {_MEMORY_TABLE} WHERE id = 1"
            )
            assert memory_revision == _MEMORY_LEGACY_REVISION, "memory 存量数据必须保留"
        finally:
            await conn.close()

        # downgrade 0014：memory 旧列重建并回填，chat 新列删除
        result = _run_bootstrap(scratch_url, "downgrade", "0014")
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        conn = await asyncpg.connect(**scratch)
        try:
            chat_columns = await _chat_image_columns(conn)
            assert not set(CHAT_IMAGE_COLUMNS) & chat_columns, "downgrade 必须删除 chat 新列"
            chat_revision = await conn.fetchval(
                f"SELECT revision FROM {_CHAT_TABLE} WHERE id = 1"
            )
            assert chat_revision == _CHAT_LEGACY_REVISION, "downgrade 不得删除 chat 存量行"

            memory_columns = await _memory_columns(conn)
            assert set(MEMORY_LEGACY_IMAGE_COLUMNS) <= memory_columns, (
                "downgrade 必须重建 memory 0014 历史图片列"
            )
            memory_row = await conn.fetchrow(
                f"SELECT vision_tool_enabled,"
                " vision_image_download_max_count,"
                " vision_image_download_max_bytes,"
                " vision_image_download_total_max_bytes,"
                " vision_image_download_max_pixels,"
                " vision_image_download_concurrency,"
                " vision_image_download_connect_timeout_seconds,"
                " vision_image_download_read_timeout_seconds,"
                " vision_image_download_total_timeout_seconds"
                f" FROM {_MEMORY_TABLE} WHERE id = 1"
            )
            assert memory_row["vision_tool_enabled"] is True, (
                "delegated 应回填为 true"
            )
            assert [
                memory_row[key] for key in _BUDGET_COLUMNS
            ] == [MEMORY_VISION_VALUES[key] for key in _BUDGET_COLUMNS], (
                "预算应从 chat 原样回填到 memory"
            )
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


@pytest.mark.skipif(
    not POSTGRES_URL, reason="未设置 KOMARI_TEST_POSTGRES_URL，跳过迁移集成测试"
)
async def test_image_columns_defaults_and_checks_at_head() -> None:
    """AC3：全新库图片列数据库默认值与跨字段 CHECK 约束生效且零漂移。"""
    if not _same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")

    scratch = await _recreate_scratch_database()
    scratch_url = _scratch_url(str(scratch["database"]))
    try:
        result = _run_bootstrap(scratch_url, "upgrade", "head")
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        conn = await asyncpg.connect(**scratch)
        try:
            # 全新 chat 行只显式提供非图片列，图片列由数据库默认补齐
            await _insert_chat_row_leaving_image_defaults(conn)
            row = await _fetched_image_row(conn)
            assert tuple(row) == tuple(
                CHAT_IMAGE_DEFAULT_VALUES[column]
                for column in CHAT_IMAGE_COLUMNS
            ), "全新行图片列应使用数据库默认值"

            # 跨字段 CHECK：总字节 >= 单图字节
            with pytest.raises(CheckViolationError):
                await conn.execute(
                    f"UPDATE {_CHAT_TABLE} SET"
                    " vision_image_download_max_bytes = 22 * 1024 * 1024,"
                    " vision_image_download_total_max_bytes = 20 * 1024 * 1024"
                    " WHERE id = 1"
                )
            # 跨字段 CHECK：总时限 >= 连接超时
            with pytest.raises(CheckViolationError):
                await conn.execute(
                    f"UPDATE {_CHAT_TABLE} SET"
                    " vision_image_download_connect_timeout_seconds = 50.0,"
                    " vision_image_download_total_timeout_seconds = 45.0"
                    " WHERE id = 1"
                )
            # 相等边界合法且不破坏后续检查
            await conn.execute(
                f"UPDATE {_CHAT_TABLE} SET"
                " vision_image_download_max_bytes = 20 * 1024 * 1024,"
                " vision_image_download_total_max_bytes = 20 * 1024 * 1024,"
                " vision_image_download_connect_timeout_seconds = 45.0,"
                " vision_image_download_total_timeout_seconds = 45.0"
                " WHERE id = 1"
            )
        finally:
            await conn.close()

        check_result = _run_bootstrap(scratch_url, "check")
        assert check_result.returncode == 0, (
            f"{check_result.stdout}\n{check_result.stderr}"
        )
    finally:
        await _drop_scratch_database(str(scratch["database"]))
