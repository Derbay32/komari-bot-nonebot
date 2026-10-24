"""TSK-190 播种侧场景退役：Prompt-only 初始化不重建旧存储（隔离库验收）。

覆盖契约要求的播种侧正向证据：

1. 全新库 ``upgrade head``（0025）后执行真实播种 CLI：三项必需 Prompt 行与
   可选判定 Prompt 行完整落库，4 张退役场景表仍不存在，且播种没有新建任何
   数据表；
2. 重复播种：Prompt 行（正文 / revision / updated_at）零变化，退役场景表
   仍不存在，数据表集合不变（幂等执行不会复活旧存储）；
3. 旧 manifest 残留 ``fixed_candidates`` / ``general_scenes`` 块时，播种仍
   成功：旧键按无关顶层键忽略，绝不据此重建、读取或写入退役表。

隔离纪律：全部用例在门控库派生的一次性隔离库内执行（唯一后缀），用例结束
即 DROP；共享门控库始终停留 head，绝不 downgrade/重建门控库；不访问真实
模型或平台服务。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import asyncpg
import pytest
import yaml

from komari_bot.config.typed_config import ensure_typed_prompt_model
from komari_bot.db.seed_bootstrap import DEFAULT_DECISION_SEED_FILE, DEFAULT_SEED_FILE
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

if TYPE_CHECKING:
    from collections.abc import Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CLI_MODULE = "komari_bot.db.seed_bootstrap"

pytestmark = [SKIP_NO_POSTGRES, pytest.mark.asyncio]

#: 已退役的 4 张场景表（test-owned 字面量，来自 TSK-190 manifest）。
RETIRED_SCENE_TABLES: tuple[str, ...] = (
    "komari_memory_scene_item",
    "komari_memory_scene_runtime",
    "komari_decision_scenes",
    "komari_memory_scene_set",
)

#: 可选判定 Prompt 资源 ID（与 `--decision-seed-file` 的资产对应）。
DECISION_RESOURCE_ID = "komari_decision"

#: 旧 manifest 残留的场景块：只验证“被忽略”，不重建任何旧存储。
LEGACY_SCENE_BLOCKS: tuple[str, ...] = (
    "fixed_candidates:",
    '  NOISE: "退役场景内容"',
    "general_scenes:",
    '  - id: "scene_retired"',
    '    text: "退役场景内容"',
)


def _run_seed_cli(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    """以公开 CLI 形式执行播种命令（需要额外选项时使用，与 prestart 同一命令）。"""
    env = os.environ.copy()
    env["SQLALCHEMY_DATABASE_URL"] = url
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    return subprocess.run(
        [sys.executable, "-m", CLI_MODULE, *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


def _scratch_suffix(tag: str) -> str:
    """每个用例唯一的隔离库后缀（重复运行与并行安全）。"""
    return f"_seedretire_{tag}_{uuid4().hex[:8]}"


def _decision_table_name() -> str:
    """可选判定 Prompt 强类型表名（来自 Schema，不硬编码）。"""
    model = ensure_typed_prompt_model(DECISION_RESOURCE_ID)
    assert model is not None, "无法加载 komari_decision Prompt 强类型 Schema"
    return str(model.__tablename__)


def _decision_asset_values() -> dict[str, str]:
    """打包可选资产中的判定正文（契约固定形状的显式 decision_prompt 键）。"""
    raw = yaml.safe_load(DEFAULT_DECISION_SEED_FILE.read_text(encoding="utf-8")) or {}
    section = raw.get("decision_prompt")
    assert isinstance(section, dict), "判定初始数据必须包含 decision_prompt 对象块"
    return {str(field): str(value) for field, value in section.items()}


async def _public_tables(connection: asyncpg.Connection) -> set[str]:
    rows = await connection.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    )
    return {str(row["tablename"]) for row in rows}


async def _assert_retired_tables_absent(connection: asyncpg.Connection) -> None:
    present = await _public_tables(connection)
    retired = set(RETIRED_SCENE_TABLES) & present
    assert not retired, f"播种不得重建已退役的场景表: {sorted(retired)}"


async def _prompt_snapshot(
    connection: asyncpg.Connection,
) -> dict[str, dict[str, Any] | None]:
    """三项必需 Prompt 行 + 可选判定 Prompt 行的完整快照。"""
    snapshot: dict[str, dict[str, Any] | None] = {}
    for resource_id in PROMPT_RESOURCE_IDS:
        snapshot[resource_id] = await fetch_prompt_row(connection, resource_id)
    row = await connection.fetchrow(
        f"SELECT * FROM {_decision_table_name()} WHERE id = 1"
    )
    snapshot[DECISION_RESOURCE_ID] = None if row is None else dict(row)
    return snapshot


def _assert_required_prompt_rows(
    snapshot: Mapping[str, dict[str, Any] | None],
) -> None:
    """三项必需 Prompt 行存在、全字段非空且等于默认资产值。"""
    for resource_id in PROMPT_RESOURCE_IDS:
        row = snapshot[resource_id]
        assert row is not None, f"seed 后 {resource_id} Prompt 单行必须存在"
        expected = asset_values(resource_id)
        for field in sorted(prompt_resource_field_names(resource_id)):
            assert str(row[field] or "").strip(), (
                f"{resource_id} Prompt 字段 {field} 不得为空"
            )
            assert row[field] == expected[field], (
                f"{resource_id} Prompt 字段 {field} 必须等于 seed 资产值"
            )
        assert row["revision"] == 1


async def _prepare_head_scratch(tag: str) -> tuple[dict[str, Any], str]:
    """重建唯一后缀的隔离库并 upgrade head；返回 (asyncpg 参数, URL)。"""
    scratch = await recreate_scratch_database(_scratch_suffix(tag))
    url = scratch_url(str(scratch["database"]))
    result = run_bootstrap(url, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    return scratch, url


async def test_prompt_only_seed_initializes_at_head_without_retired_tables() -> None:
    """全新 head 库：只播 Prompt，退役场景表缺席且不新建任何数据表。"""
    if not same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")

    scratch, url = await _prepare_head_scratch("fresh")
    try:
        connection = await asyncpg.connect(**scratch)
        try:
            assert (
                await connection.fetchval("SELECT version_num FROM alembic_version")
                == HEAD_REVISION
            ), f"隔离库必须停在单一 head {HEAD_REVISION}"
            tables_before = await _public_tables(connection)
            await _assert_retired_tables_absent(connection)
        finally:
            await connection.close()

        result = run_seed(url)
        output = f"{result.stdout}\n{result.stderr}"
        assert result.returncode == 0, output
        assert "新建 4 行" in output, (
            f"播种报告必须确认三项必需 Prompt 与可选判定 Prompt 均新建单行: {output}"
        )

        connection = await asyncpg.connect(**scratch)
        try:
            snapshot = await _prompt_snapshot(connection)
            _assert_required_prompt_rows(snapshot)

            decision_row = snapshot[DECISION_RESOURCE_ID]
            assert decision_row is not None, "seed 后判定 Prompt 单行必须存在"
            expected_decision = _decision_asset_values()
            for field, expected in expected_decision.items():
                assert decision_row[field] == expected, (
                    f"判定 Prompt 字段 {field} 必须等于打包资产值"
                )
            assert decision_row["revision"] == 1

            await _assert_retired_tables_absent(connection)
            tables_after = await _public_tables(connection)
            assert tables_after == tables_before, (
                "播种不得新建或删除数据表: "
                f"新增={sorted(tables_after - tables_before)} "
                f"消失={sorted(tables_before - tables_after)}"
            )
        finally:
            await connection.close()
    finally:
        await drop_scratch_database(str(scratch["database"]))


async def test_repeated_seed_does_not_recreate_storage_or_change_prompt_rows() -> None:
    """重复播种：Prompt 行零变化，退役场景表不复活，数据表集合不变。"""
    if not same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")

    scratch, url = await _prepare_head_scratch("repeat")
    try:
        result = run_seed(url)
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

        connection = await asyncpg.connect(**scratch)
        try:
            before_rows = await _prompt_snapshot(connection)
            _assert_required_prompt_rows(before_rows)
            tables_before = await _public_tables(connection)

            result = run_seed(url)
            assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

            after_rows = await _prompt_snapshot(connection)
            assert after_rows == before_rows, (
                "重复播种不得改写任何 Prompt 行的正文、revision 或 updated_at"
            )
            await _assert_retired_tables_absent(connection)
            assert await _public_tables(connection) == tables_before, (
                "重复播种不得新建数据表"
            )
        finally:
            await connection.close()
    finally:
        await drop_scratch_database(str(scratch["database"]))


async def test_manifest_with_retired_scene_blocks_is_seeded_and_ignored(
    tmp_path: Path,
) -> None:
    """旧 manifest 残留场景块仍只播 Prompt：旧键被忽略，退役表不重建。"""
    if not same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")

    seed_file = tmp_path / "legacy-scene-manifest.yaml"
    body = DEFAULT_SEED_FILE.read_text(encoding="utf-8")
    seed_file.write_text(
        body + "\n" + "\n".join(LEGACY_SCENE_BLOCKS) + "\n",
        encoding="utf-8",
    )

    scratch, url = await _prepare_head_scratch("legacykeys")
    try:
        result = _run_seed_cli(url, "--seed-file", str(seed_file))
        output = f"{result.stdout}\n{result.stderr}"
        assert result.returncode == 0, output

        connection = await asyncpg.connect(**scratch)
        try:
            _assert_required_prompt_rows(await _prompt_snapshot(connection))
            await _assert_retired_tables_absent(connection)
        finally:
            await connection.close()
    finally:
        await drop_scratch_database(str(scratch["database"]))
