"""TSK-197 验收 1/3/4 —— 全新数据库协调式升级门禁（PostgreSQL 集成）。

本文件是 TSK-197（最终 integrate-and-verify gate）的协调式全新数据库
验收：在同一个一次性隔离库内按生产顺序完成「Alembic 升级 → 模型元数据
零漂移检查 → 初始数据播种 → 冷启动完整性验证 → 重复播种幂等 → 再次零漂移
检查」，而不是把升级、播种、校验拆散到互不相关的测试里。

覆盖（对应验收标准）：
- AC1：从基线重建的全新数据库可依次完成 Alembic 升级（head = 0033）、
  初始数据播种与冷启动完整性验证（可选 Jev Prompt 单行）；
- AC3：迁移链单一 head 且 ``orm_bootstrap check`` 零漂移（升级后与
  播种后各一次）；
- AC4：可选 Jev Prompt 在新库中完整初始化；重复播种
  不产生无意义 revision 变化；4 张退役场景表在播种前后都不存在。

隔离纪律：用例在从门控 DSN 派生的一次性隔离库（库名后缀
``_tsk197fresh``）内执行，用例结束即 DROP；共享门控库始终保持 head。
门控用户需要 CREATEDB 权限。无 ``KOMARI_TEST_POSTGRES_URL`` 或与
``SQLALCHEMY_DATABASE_URL`` 不同库时按既有约定 skip，不硬编码凭据。
"""

from __future__ import annotations

from typing import Any

import asyncpg
import pytest
import yaml

from komari_bot.db.seed_bootstrap import DEFAULT_DECISION_SEED_FILE
from tests.config.prompt_field_contract import (
    PROMPT_RESOURCE_IDS,
    prompt_resource_field_names,
)
from tests.db.tsk197_gate_support import (
    HEAD_REVISION,
    POSTGRES_URL,
    SKIP_NO_POSTGRES,
    SQLALCHEMY_URL,
    asset_values,
    drop_scratch_database,
    fetch_prompt_row,
    recreate_scratch_database,
    run_bootstrap,
    run_seed,
    same_database,
    scratch_url,
)

pytestmark = [SKIP_NO_POSTGRES, pytest.mark.asyncio]

#: 已退役的 4 张场景表（test-owned 字面量，来自 TSK-190 manifest）。
RETIRED_SCENE_TABLES: tuple[str, ...] = (
    "komari_memory_scene_item",
    "komari_memory_scene_runtime",
    "komari_decision_scenes",
    "komari_memory_scene_set",
)


async def _public_tables(connection: asyncpg.Connection) -> set[str]:
    rows = await connection.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    )
    return {str(row["tablename"]) for row in rows}


async def _assert_retired_tables_absent(connection: asyncpg.Connection) -> None:
    present = await _public_tables(connection)
    retired = set(RETIRED_SCENE_TABLES) & present
    assert not retired, f"播种不得重建已退役的场景表: {sorted(retired)}"


async def test_fresh_database_completes_upgrade_seed_and_cold_start_gate() -> None:
    """AC1/AC3/AC4：全新库走完升级→检查→播种→冷启动→幂等→检查。"""
    if not same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")

    scratch = await recreate_scratch_database("_tsk197fresh")
    db_url = scratch_url(str(scratch["database"]))
    try:
        # 1. 全新数据库升级到 head，单一 head
        result = run_bootstrap(db_url, "upgrade", "head")
        assert result.returncode == 0, result.stderr

        connection = await asyncpg.connect(**scratch)
        try:
            assert (
                await connection.fetchval("SELECT version_num FROM alembic_version")
                == HEAD_REVISION
            ), "全新库升级后必须停留在单一 head"
        finally:
            await connection.close()

        # 2. 升级后模型元数据零漂移
        check_result = run_bootstrap(db_url, "check")
        assert check_result.returncode == 0, (
            f"{check_result.stdout}\n{check_result.stderr}"
        )

        # 3. 初始数据播种（可选 Jev Prompt）
        result = run_seed(db_url)
        output = f"{result.stdout}\n{result.stderr}"
        assert result.returncode == 0, output
        assert "新建 1 行" in output, (
            f"播种报告必须确认可选 Jev Prompt 新建单行: {output}"
        )

        connection = await asyncpg.connect(**scratch)
        try:
            # 4. 场景初始化已退役：4 张退役场景表不得存在
            await _assert_retired_tables_absent(connection)

            # 5. 冷启动完整性：可选 Jev Prompt 单行完整且等于 seed 资产值
            before_rows: dict[str, dict[str, Any] | None] = {}
            for resource_id in PROMPT_RESOURCE_IDS:
                row = await fetch_prompt_row(connection, resource_id)
                assert row is not None, f"seed 后 {resource_id} Prompt 单行必须存在"
                schema_fields = prompt_resource_field_names(resource_id)
                assert set(row) - {"id", "revision", "updated_at"} == schema_fields
                expected_values = asset_values(resource_id)
                for field in sorted(schema_fields):
                    assert row[field] == expected_values[field], (
                        f"{resource_id} Prompt 字段 {field} 必须等于 seed 资产值"
                    )
                assert row["revision"] == 1, (
                    f"新库 {resource_id} Prompt 行 revision 应为 1"
                )
                before_rows[resource_id] = row

            # 默认可选资产存在时，判定 Prompt 也必须完整落库。
            decision_values = yaml.safe_load(
                DEFAULT_DECISION_SEED_FILE.read_text(encoding="utf-8")
            )["decision_prompt"]
            assert set(decision_values) == {
                "role_description",
                "reply_eligibility",
                "retention_purpose",
                "summary_recognition",
            }
            decision_before = await connection.fetchrow(
                "SELECT * FROM komari_prompt_komari_decision WHERE id = 1"
            )
            assert decision_before is not None
            assert decision_before["revision"] == 1
            for field, expected in decision_values.items():
                assert expected.strip()
                assert decision_before[field] == expected

            # 6. 重复播种幂等：无 revision/content 变化
            result = run_seed(db_url)
            assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
            for resource_id in PROMPT_RESOURCE_IDS:
                after = await fetch_prompt_row(connection, resource_id)
                assert after == before_rows[resource_id], (
                    f"重复播种不得产生 {resource_id} revision/content 变化"
                )
            assert (
                await connection.fetchrow(
                    "SELECT * FROM komari_prompt_komari_decision WHERE id = 1"
                )
                == decision_before
            )
            await _assert_retired_tables_absent(connection)
        finally:
            await connection.close()

        # 7. 播种后再次模型元数据零漂移
        check_result = run_bootstrap(db_url, "check")
        assert check_result.returncode == 0, (
            f"{check_result.stdout}\n{check_result.stderr}"
        )
    finally:
        await drop_scratch_database(str(scratch["database"]))
