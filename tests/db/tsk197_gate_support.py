"""TSK-197 协调式升级门禁共享测试支持（test-support，不收集为用例）。

两个 TSK-197 DB gate（``test_tsk197_fresh_database_gate`` /
``test_tsk197_legacy_upgrade_gate``）共用的一次性隔离库与迁移/播种
基础设施：

- 解析门控 DSN、重建/删除一次性隔离库（``recreate_scratch_database``
  接受唯一后缀，支持参数化/并行/重复运行安全）、运行 orm_bootstrap 与
  seed_bootstrap、seed 资产定位与 Prompt 行读取、门控 skip 标记与 head
  版本常量；
- 旧库升级 gate 专属的存量行夹具（``insert_row_without_budget_columns``
  与 memory 图片列常量/占位值）也收敛在此，避免从既有测试模块导入
  underscore helper。

只抽取本 ticket 两个 gate 实际使用的稳定测试基础设施，不做 speculative
generality；断言全部留在各 gate 文件。隔离纪律：用例在门控库派生的一次性
隔离库（库名 = 门控库 + suffix）内执行，由调用方 finally DROP；共享门控库
始终保持 head。无 ``KOMARI_TEST_POSTGRES_URL`` 或与
``SQLALCHEMY_DATABASE_URL`` 不同库时按既有约定 skip，不硬编码凭据。
"""

from __future__ import annotations

import asyncio
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
from asyncpg.exceptions import InsufficientPrivilegeError

from komari_bot.db.seed_bootstrap import DEFAULT_SEED_FILE
from tests.config.prompt_field_contract import (
    find_prompt_mapping,
    prompt_table_name,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")
SQLALCHEMY_URL = os.getenv("SQLALCHEMY_DATABASE_URL", "")

#: 无门控数据库时跳过集成测试（两个 gate 共用同一守卫）。
SKIP_NO_POSTGRES = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="未设置 KOMARI_TEST_POSTGRES_URL，跳过集成测试",
)

HEAD_REVISION = "0015"

#: 0013 引入的三项回复 Agent 预算列（仅让这些列走数据库默认值）。
AGENT_BUDGET_COLUMNS = (
    "agent_max_rounds",
    "agent_max_tool_calls_per_round",
    "agent_max_total_tool_calls",
)

#: 除预算列外 komari_chat_config 现有列的默认值（与 schema 与迁移链一致），
#: 用于构造“只让预算列走数据库默认值”的测试行。
NON_BUDGET_COLUMN_DEFAULTS: dict[str, object] = {
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

#: 8 项图片下载预算列（chat head 与 memory 0014 历史两集合共有的部分）。
BUDGET_COLUMNS: tuple[str, ...] = (
    "vision_image_download_max_count",
    "vision_image_download_max_bytes",
    "vision_image_download_total_max_bytes",
    "vision_image_download_max_pixels",
    "vision_image_download_concurrency",
    "vision_image_download_connect_timeout_seconds",
    "vision_image_download_read_timeout_seconds",
    "vision_image_download_total_timeout_seconds",
)

#: memory 0014 历史图片列集合：vision_tool_enabled + 8 项预算（0002 建列，
#: 0015 从 memory 删除；downgrade 0014 必须重建这 9 列）。
MEMORY_LEGACY_IMAGE_COLUMNS: tuple[str, ...] = (
    "vision_tool_enabled",
    *BUDGET_COLUMNS,
)

#: 0014 时代 memory 旧图片列的自定义值（迁移后应原样落到 chat；预算组合
#: 满足 chat 的跨字段 CHECK：总字节 >= 单图字节、总时限 >= 连接超时）。
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

#: 0014 时代 memory 单行使用的 revision（区别于 chat 的 revision，验证
#: 存量数据不被迁移触碰）。
MEMORY_LEGACY_REVISION = 77
CHAT_LEGACY_REVISION = 42


def same_database(left: str, right: str) -> bool:
    """两个连接串是否指向同一个数据库（主机/端口/库名一致）。"""
    left_parsed = urlparse(left)
    right_parsed = urlparse(right)
    return (
        left_parsed.hostname == right_parsed.hostname
        and (left_parsed.port or 5432) == (right_parsed.port or 5432)
        and left_parsed.path == right_parsed.path
    )


def parse_dsn(url: str) -> dict[str, Any]:
    """把 asyncpg/SQLAlchemy 连接串解析为 asyncpg 连接参数。"""
    parsed = urlparse(url.replace("postgresql+asyncpg://", "postgresql://"))
    return {
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "database": parsed.path.lstrip("/"),
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
    }


def run_bootstrap(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    """在仓库根目录执行 orm_bootstrap 迁移命令（隔离库 URL 覆盖环境变量）。"""
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


def run_seed(url: str) -> subprocess.CompletedProcess[str]:
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


def scratch_url(database: str) -> str:
    """把门控 DSN 的库名替换为隔离库名，其余连接参数保持不变。"""
    return urlparse(POSTGRES_URL)._replace(path=f"/{database}").geturl()


async def _drop_database_if_exists(
    connection: asyncpg.Connection,
    name: str,
) -> None:
    """DROP DATABASE IF EXISTS（FORCE），带 stale 后端回收退避。

    FORCE 需要终止目标库所有活动后端；门控角色（CREATEDB、非 superuser）
    只能终止自己/同角色后端，遇到其他角色残留后端（如子进程退出后尚未被
    服务端回收的连接）会偶发 InsufficientPrivilegeError。这里对该特定错误
    短退避重试（1s/2s），等服务端处理完残留后端的 socket 关闭后再删；重试
    仍失败则原样上抛，绝不静默吞掉清理失败。无残留后端时 FORCE 首次即成功。
    """
    statement = f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'
    for attempt in range(3):
        try:
            await connection.execute(statement)
        except InsufficientPrivilegeError:
            if attempt == 2:
                raise
            await asyncio.sleep(1.0 * (attempt + 1))
        else:
            return


async def recreate_scratch_database(suffix: str) -> dict[str, Any]:
    """重建后缀唯一的一次性隔离库并返回其 asyncpg 连接参数。

    隔离库名 = 门控库名 + ``suffix``；先 DROP（FORCE 断开残留连接）再
    CREATE，重复执行与参数化并行运行安全。suffix 必须由调用方保证唯一
    （每个用例一个，绝不共用 5432/6379 默认服务）。
    """
    base = parse_dsn(POSTGRES_URL)
    scratch = {**base, "database": f"{base['database']}{suffix}"}
    connection = await asyncpg.connect(**base)
    try:
        name = str(scratch["database"]).replace('"', '""')
        await _drop_database_if_exists(connection, name)
        await connection.execute(f'CREATE DATABASE "{name}"')
    finally:
        await connection.close()
    return scratch


async def drop_scratch_database(database: str) -> None:
    """删除一次性隔离库（finally 清理，重复删除安全）。"""
    base = parse_dsn(POSTGRES_URL)
    connection = await asyncpg.connect(**base)
    try:
        name = database.replace('"', '""')
        await _drop_database_if_exists(connection, name)
    finally:
        await connection.close()


def asset_values(resource_id: str) -> dict[str, str]:
    """从默认 seed 资产读取指定 Prompt 资源的初始值（通用定位，不锁定布局）。"""
    raw = yaml.safe_load(DEFAULT_SEED_FILE.read_text(encoding="utf-8")) or {}
    mapping = find_prompt_mapping(raw, resource_id)
    assert mapping is not None, f"默认 seed 资产缺少 {resource_id} Prompt 初始数据块"
    return {str(field): str(value) for field, value in mapping.items()}


async def fetch_prompt_row(
    connection: asyncpg.Connection,
    resource_id: str,
) -> dict[str, Any] | None:
    """读取指定 Prompt 资源 id=1 单行；无行返回 ``None``。"""
    table = prompt_table_name(resource_id)
    row = await connection.fetchrow(f"SELECT * FROM {table} WHERE id = 1")
    return None if row is None else dict(row)


async def insert_row_without_budget_columns(
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
        column for column in value_columns if column not in NON_BUDGET_COLUMN_DEFAULTS
    ]
    assert not missing, f"测试默认值字典缺少列: {missing}"
    columns_sql = ", ".join(["id", "revision", "updated_at", *value_columns])
    placeholders = ", ".join(f"${index}" for index in range(1, 4 + len(value_columns)))
    await connection.execute(
        f"INSERT INTO komari_chat_config ({columns_sql}) VALUES ({placeholders})",
        1,
        revision,
        datetime.now(UTC),
        *[NON_BUDGET_COLUMN_DEFAULTS[column] for column in value_columns],
    )


def memory_row_value(data_type: str, *, nullable: bool) -> object:
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
