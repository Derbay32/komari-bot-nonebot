"""TSK-189/TSK-190 版本化初始数据播种 —— PostgreSQL 集成验收。

依赖已执行 ``alembic upgrade head`` 的迁移管理 schema（``KOMARI_TEST_POSTGRES_URL``
门控，且与 ``SQLALCHEMY_DATABASE_URL`` 同库，否则按既有约定 skip）。

隔离纪律：集成用例在从门控 DSN 派生的一次性隔离库（库名后缀
``_seedscenes``）内先 ``upgrade head`` 再执行公开播种 CLI
（``python -m komari_bot.db.seed_bootstrap``），用例结束即 DROP；
共享门控库的版本与数据不受影响，重复执行与执行顺序互不影响。
门控用户需要 CREATEDB 权限。

TSK-190 起场景初始化退役，本文件只保留失败输入的无部分写入验收：

- 非法 seed 文件明确失败且指明出错文件；
- 失败后所有必需与可选 Prompt 表都没有新增行；
- 失败后 4 张退役场景表仍不存在（播种不会重建旧存储）。

成功路径的 Prompt 初始化与幂等性由 ``test_prompt_seed_bootstrap_integration``
覆盖；退役存储不复活的正向证据由 ``test_seed_scene_retirement`` 覆盖。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse

import asyncpg
import pytest

from komari_bot.config.typed_config import ensure_typed_prompt_model
from tests.config.prompt_field_contract import (
    PROMPT_RESOURCE_IDS,
    prompt_table_name,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[2]

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")
SQLALCHEMY_URL = os.getenv("SQLALCHEMY_DATABASE_URL", "")

#: 已退役的 4 张场景表（test-owned 字面量，来自 TSK-190 manifest）。
RETIRED_SCENE_TABLES: tuple[str, ...] = (
    "komari_memory_scene_item",
    "komari_memory_scene_runtime",
    "komari_decision_scenes",
    "komari_memory_scene_set",
)

#: 可选判定 Prompt 资源 ID（与 `--decision-seed-file` 的资产对应）。
DECISION_RESOURCE_ID = "komari_decision"


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
    """在仓库根目录执行 orm_bootstrap 迁移命令（隔离库 URL 经环境变量覆盖）。"""
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


def _run_seed(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    """以公开 CLI 形式执行播种命令（与 prestart / 本地 / CI 同一命令）。"""
    env = os.environ.copy()
    env["SQLALCHEMY_DATABASE_URL"] = url
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "komari_bot.db.seed_bootstrap", *args],
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

    隔离库名 = 门控库名 + ``_seedscenes``；先 DROP（FORCE 断开残留
    连接）再 CREATE，重复执行幂等。
    """
    base = _parse_dsn(POSTGRES_URL)
    scratch = {**base, "database": f"{base['database']}_seedscenes"}
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


async def _prepare_head_scratch() -> tuple[dict[str, Any], str]:
    """重建隔离库并 upgrade head；返回 (scratch 连接参数, scratch URL)。"""
    scratch = await _recreate_scratch_database()
    scratch_url = _scratch_url(str(scratch["database"]))
    result = _run_bootstrap(scratch_url, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    return scratch, scratch_url


def _prompt_tables() -> tuple[str, ...]:
    """必需三个 Prompt 表 + 可选判定 Prompt 表（表名来自强类型 Schema）。"""
    decision_model = ensure_typed_prompt_model(DECISION_RESOURCE_ID)
    assert decision_model is not None, "无法加载 komari_decision Prompt Schema"
    return (
        *(prompt_table_name(resource_id) for resource_id in PROMPT_RESOURCE_IDS),
        str(decision_model.__tablename__),
    )


async def _public_tables(connection: asyncpg.Connection) -> set[str]:
    rows = await connection.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    )
    return {str(row["tablename"]) for row in rows}


async def _assert_prompt_tables_empty(
    connection: asyncpg.Connection,
    tables: Iterable[str],
) -> None:
    for table in tables:
        count = await connection.fetchval(f"SELECT count(*) FROM {table}")
        assert count == 0, f"失败输入不得写入 {table}（实际 {count} 行）"


@pytest.mark.skipif(
    not POSTGRES_URL, reason="未设置 KOMARI_TEST_POSTGRES_URL，跳过集成测试"
)
async def test_seed_invalid_file_fails_and_leaves_database_untouched(
    tmp_path: Path,
) -> None:
    """AC6：非法 seed 文件明确失败，且数据库无任何部分写入。

    失败后三项必需 Prompt 表与可选判定 Prompt 表都必须为空，且 4 张退役
    场景表必须仍不存在：失败路径不得重建旧存储。
    """
    if not _same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")

    scratch, scratch_url = await _prepare_head_scratch()
    try:
        bad_file = tmp_path / "bad-seed.yaml"
        bad_file.write_text("- just\n- a list\n", encoding="utf-8")

        result = _run_seed(scratch_url, "--seed-file", str(bad_file))
        output = f"{result.stdout}\n{result.stderr}"
        assert result.returncode != 0, output
        assert str(bad_file) in output, "报错必须指明出错的 seed 文件"

        connection = await asyncpg.connect(**scratch)
        try:
            await _assert_prompt_tables_empty(connection, _prompt_tables())
            present = await _public_tables(connection)
            assert not set(RETIRED_SCENE_TABLES) & present, (
                "失败输入不得重建已退役的场景表: "
                f"{sorted(set(RETIRED_SCENE_TABLES) & present)}"
            )
        finally:
            await connection.close()
    finally:
        await _drop_scratch_database(str(scratch["database"]))
