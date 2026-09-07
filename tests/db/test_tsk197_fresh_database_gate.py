"""TSK-197 验收 1/3/4 —— 全新数据库协调式升级门禁（PostgreSQL 集成）。

本文件是 TSK-197（最终 integrate-and-verify gate）的协调式全新数据库
验收：在同一个一次性隔离库内按生产顺序完成「Alembic 升级 → 模型元数据
零漂移检查 → 初始数据播种 → 冷启动完整性验证 → 重复播种幂等 → 再次零漂移
检查」，而不是把升级、播种、校验拆散到互不相关的测试里。

覆盖（对应验收标准）：
- AC1：从基线重建的全新数据库可依次完成 Alembic 升级（head = 0018）、
  初始数据播种与冷启动完整性验证（三个 Prompt 资源单行 + 判定场景）；
- AC3：迁移链单一 head 且 ``orm_bootstrap check`` 零漂移（升级后与
  播种后各一次）；
- AC4：三个 Prompt 资源与判定场景在新库中完整初始化；重复播种不产生
  无意义 revision 变化。

隔离纪律：用例在从门控 DSN 派生的一次性隔离库（库名后缀
``_tsk197fresh``）内执行，用例结束即 DROP；共享门控库始终保持 head。
门控用户需要 CREATEDB 权限。无 ``KOMARI_TEST_POSTGRES_URL`` 或与
``SQLALCHEMY_DATABASE_URL`` 不同库时按既有约定 skip，不硬编码凭据。
"""

from __future__ import annotations

from typing import Any

import asyncpg
import pytest

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
from tests.komari_decision.required_fixed_scene_keys import REQUIRED_FIXED_SCENE_KEYS

pytestmark = [SKIP_NO_POSTGRES, pytest.mark.asyncio]


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
            assert await connection.fetchval(
                "SELECT version_num FROM alembic_version"
            ) == HEAD_REVISION, "全新库升级后必须停留在单一 head 0018"
        finally:
            await connection.close()

        # 2. 升级后模型元数据零漂移
        check_result = run_bootstrap(db_url, "check")
        assert check_result.returncode == 0, (
            f"{check_result.stdout}\n{check_result.stderr}"
        )

        # 3. 初始数据播种（三个 Prompt 资源 + 判定场景）
        result = run_seed(db_url)
        output = f"{result.stdout}\n{result.stderr}"
        assert result.returncode == 0, output
        assert "新建 3 行" in output, (
            f"播种报告必须确认三个 Prompt 资源均新建单行: {output}"
        )

        connection = await asyncpg.connect(**scratch)
        try:
            # 4. 冷启动完整性：必需 fixed 场景 + 至少一个启用的一般场景
            fixed_rows = await connection.fetch(
                "SELECT scene_key, scene_type, enabled, content_text"
                " FROM komari_decision_scenes WHERE scene_type = 'fixed'"
            )
            fixed_by_key = {row["scene_key"]: row for row in fixed_rows}
            assert set(fixed_by_key) == set(REQUIRED_FIXED_SCENE_KEYS), (
                f"播种后必需固定场景不齐: {sorted(fixed_by_key)}"
            )
            assert all(row["enabled"] for row in fixed_rows)
            assert all(str(row["content_text"]).strip() for row in fixed_rows)
            general_count = await connection.fetchval(
                "SELECT count(*) FROM komari_decision_scenes"
                " WHERE scene_type = 'general' AND enabled = true"
            )
            assert general_count >= 1, "播种后缺少启用的一般场景"

            # 5. 冷启动完整性：三个 Prompt 资源单行完整且等于 seed 资产值
            before_rows: dict[str, dict[str, Any] | None] = {}
            for resource_id in PROMPT_RESOURCE_IDS:
                row = await fetch_prompt_row(connection, resource_id)
                assert row is not None, (
                    f"seed 后 {resource_id} Prompt 单行必须存在"
                )
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

            # 6. 重复播种幂等：无 revision/content 变化
            result = run_seed(db_url)
            assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
            for resource_id in PROMPT_RESOURCE_IDS:
                after = await fetch_prompt_row(connection, resource_id)
                assert after == before_rows[resource_id], (
                    f"重复播种不得产生 {resource_id} revision/content 变化"
                )
            scene_count = await connection.fetchval(
                "SELECT count(*) FROM komari_decision_scenes"
            )
            fixed_count = await connection.fetchval(
                "SELECT count(*) FROM komari_decision_scenes"
                " WHERE scene_type = 'fixed'"
            )
            assert scene_count == fixed_count + general_count, (
                "重复播种不得重复插入场景"
            )
        finally:
            await connection.close()

        # 7. 播种后再次模型元数据零漂移
        check_result = run_bootstrap(db_url, "check")
        assert check_result.returncode == 0, (
            f"{check_result.stdout}\n{check_result.stderr}"
        )
    finally:
        await drop_scratch_database(str(scratch["database"]))
