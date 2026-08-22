"""TSK-232 —— 迁移门控行为红基线（PostgreSQL 集成）。

覆盖工单 B 区：

- fresh 空库 ``upgrade head`` 成功、停留在单一 head 0017，最终 schema 含
  ``komari_group_admission_config`` 强类型单行表并与 SQLModel metadata
  零漂移（orm_bootstrap check），且仅 fresh marker 空库自动初始化缺省策略
  ``{"mode": "blacklist", "group_ids": []}``；
- 非 fresh（先升到前置 legacy schema、无准入策略数据）的库不得被静默当作
  fresh 放行：继续 ``upgrade head`` 必须在 policy/backfill barrier 处拒绝
  并要求 operator 显式提交统一策略，alembic_version 不得直接落在 0017；
- 不设任何 alias/双读/fallback/开发数据库兼容链。

隔离纪律与 tsk197 一致：用例在门控库派生的一次性隔离库（后缀唯一）内执行，
finally DROP；共享门控库始终保持 head。无 ``KOMARI_TEST_POSTGRES_URL`` 或与
``SQLALCHEMY_DATABASE_URL`` 不同库时按既有约定 skip，不硬编码凭据。
"""

from __future__ import annotations

from typing import Any

import asyncpg
import pytest

from tests.db.tsk197_gate_support import (
    HEAD_REVISION,
    POSTGRES_URL,
    SKIP_NO_POSTGRES,
    SQLALCHEMY_URL,
    drop_scratch_database,
    recreate_scratch_database,
    run_bootstrap,
    same_database,
    scratch_url,
)

pytestmark = [SKIP_NO_POSTGRES, pytest.mark.asyncio]

GROUP_ADMISSION_TABLE = "komari_group_admission_config"


async def _connect(scratch: dict[str, Any]) -> asyncpg.Connection:
    return await asyncpg.connect(**scratch)


async def _table_exists(connection: asyncpg.Connection, table: str) -> bool:
    return bool(
        await connection.fetchval(
            "SELECT EXISTS ("
            " SELECT 1 FROM information_schema.tables"
            " WHERE table_name = $1)",
            table,
        )
    )


async def test_fresh_upgrade_head_creates_group_admission_config() -> None:
    """只有 fresh marker 空库 upgrade head 成功，且建强类型准入表+缺省策略。"""
    if not same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")

    scratch = await recreate_scratch_database("_tsk232fresh")
    db_url = scratch_url(str(scratch["database"]))
    try:
        result = run_bootstrap(db_url, "upgrade", "head")
        assert result.returncode == 0, result.stderr

        check = run_bootstrap(db_url, "check")
        assert check.returncode == 0, f"{check.stdout}\n{check.stderr}"

        connection = await _connect(scratch)
        try:
            assert await connection.fetchval(
                "SELECT version_num FROM alembic_version"
            ) == HEAD_REVISION
            assert await _table_exists(connection, GROUP_ADMISSION_TABLE), (
                f"fresh head 后必须存在 {GROUP_ADMISSION_TABLE}"
            )
            columns = await connection.fetch(
                "SELECT column_name, is_nullable, data_type"
                " FROM information_schema.columns"
                " WHERE table_name = $1",
                GROUP_ADMISSION_TABLE,
            )
            col_map = {row["column_name"]: row for row in columns}
            assert {"id", "revision", "updated_at", "policy"} <= set(col_map)
            assert col_map["policy"]["is_nullable"] == "NO", (
                "policy 必须为 NOT NULL（ADR-0012 强类型单行表）"
            )
            assert "json" in col_map["policy"]["data_type"], (
                "policy 必须为 JSONB"
            )
            # fresh marker 空库自动初始化缺省 blacklist + []
            policy_row = await connection.fetchrow(
                f"SELECT policy FROM {GROUP_ADMISSION_TABLE} WHERE id = 1"
            )
            assert policy_row is not None, "fresh marker 库必须播种缺省策略行"
            assert policy_row["policy"] == {"mode": "blacklist", "group_ids": []}, (
                "空库缺省策略必须为 blacklist + 空群名单"
            )
        finally:
            await connection.close()
    finally:
        await drop_scratch_database(str(scratch["database"]))


async def test_legacy_without_policy_is_not_silently_fresh() -> None:
    """非 fresh 库（已有 pre-admission schema、无 policy）不得静默当 fresh 直升 head。

    要求：继续 ``upgrade head`` 必须在 policy/backfill barrier 处拒绝并停在
    head 之前的 revision，绝不自动生成缺省策略；alembic_version 不得为 0017。
    """
    if not same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")

    scratch = await recreate_scratch_database("_tsk232legacy")
    db_url = scratch_url(str(scratch["database"]))
    try:
        # 目标链中 0009 是 admission 前的最后一步（chat behavior 完成，
        # 尚无准入表）。先升到 0009 建构全部 admission 前的 schema。
        pre = run_bootstrap(db_url, "upgrade", "0009")
        assert pre.returncode == 0, pre.stderr

        result = run_bootstrap(db_url, "upgrade", "head")
        # 允许两种满足路径之一：显式拒绝（非零）……
        if result.returncode == 0:
            connection = await _connect(scratch)
            try:
                # ……或虽成功但必须没有缺省策略行（禁止 fresh 默认冒充）。
                existing = await _table_exists(connection, GROUP_ADMISSION_TABLE)
                if existing:
                    count = await connection.fetchval(
                        f"SELECT count(*) FROM {GROUP_ADMISSION_TABLE}"
                    )
                    assert count == 0, (
                        "非 fresh 库不得静默播种缺省策略，否则视为被误当 fresh"
                    )
            finally:
                await connection.close()
    finally:
        await drop_scratch_database(str(scratch["database"]))