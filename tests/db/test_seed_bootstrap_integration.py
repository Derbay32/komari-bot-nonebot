"""TSK-189 版本化初始数据播种 —— PostgreSQL 集成验收。

依赖已执行 ``alembic upgrade head`` 的迁移管理 schema（``KOMARI_TEST_POSTGRES_URL``
门控，且与 ``SQLALCHEMY_DATABASE_URL`` 同库，否则按既有约定 skip）。

隔离纪律：集成用例在从门控 DSN 派生的一次性隔离库（库名后缀
``_seedscenes``）内先 ``upgrade head`` 再执行公开播种 CLI
（``python -m komari_bot.db.seed_bootstrap``），用例结束即 DROP；
共享门控库的版本与数据不受影响，重复执行与执行顺序互不影响。
门控用户需要 CREATEDB 权限。

覆盖清单（对应验收标准）：
- 全新数据库：四个必需固定场景 + 至少一个一般场景 + 可构建运行时快照（AC3）；
- 部分已有 + 管理员自定义：按 scene_key 只补缺失、不覆盖/删除已有内容（AC4）；
- 重复执行：不重复、不改已有内容（AC5）；
- 失败输入：非法 seed 文件明确失败且数据库无部分写入（AC6 的 DB 侧确认）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import unquote, urlparse

import asyncpg
import pytest

from komari_bot.plugins.komari_decision.services.scene_runtime_service import (
    SceneRuntimeService,
)
from tests.komari_decision.required_fixed_scene_keys import REQUIRED_FIXED_SCENE_KEYS

if TYPE_CHECKING:
    from komari_bot.plugins.komari_decision.services.scene_runtime_service import (
        SceneRuntimeSnapshot,
    )

PROJECT_ROOT = Path(__file__).resolve().parents[2]

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")
SQLALCHEMY_URL = os.getenv("SQLALCHEMY_DATABASE_URL", "")


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


class _ScratchRuntimeRepository:
    """只实现 SceneRuntimeService 依赖的两个只读接口（asyncpg 直连隔离库）。"""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def get_active_set(self) -> dict[str, Any] | None:
        row = await self._conn.fetchrow(
            "SELECT s.id, s.status, r.updated_at AS runtime_updated_at"
            " FROM komari_memory_scene_runtime AS r"
            " JOIN komari_memory_scene_set AS s ON s.id = r.active_set_id"
            " WHERE r.id = 1"
        )
        if row is None:
            return None
        return dict(row)

    async def list_items_by_set(
        self,
        set_id: int,
        status: str | None = None,
        *,
        enabled_only: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        rows = await self._conn.fetch(
            "SELECT id, scene_key_snapshot AS scene_key,"
            " scene_type_snapshot AS scene_type,"
            " content_text_snapshot AS content_text,"
            " enabled_snapshot AS enabled, order_index_snapshot AS order_index,"
            " content_hash, embedding, status"
            " FROM komari_memory_scene_item"
            " WHERE set_id = $1 ORDER BY order_index_snapshot, id",
            set_id,
        )
        items = [dict(row) for row in rows]
        if status is not None:
            items = [item for item in items if item["status"] == status]
        if enabled_only:
            items = [item for item in items if item["enabled"]]
        if limit is not None:
            items = items[:limit]
        return items


async def _insert_scene(
    conn: asyncpg.Connection,
    *,
    scene_key: str,
    scene_type: str,
    content_text: str,
    content_hash: str,
    enabled: bool,
    order_index: int,
) -> None:
    """在隔离库中直接预置 scene 内容行（测试夹具）。"""
    await conn.execute(
        "INSERT INTO komari_decision_scenes"
        " (scene_key, scene_type, content_text, content_hash, enabled,"
        "  order_index) VALUES ($1, $2, $3, $4, $5, $6)",
        scene_key,
        scene_type,
        content_text,
        content_hash,
        enabled,
        order_index,
    )


async def _build_runtime_snapshot(
    conn: asyncpg.Connection,
) -> "SceneRuntimeSnapshot | None":
    """在已播种的隔离库内构造 READY set 并调用公开运行时服务构建快照。"""
    set_id = await conn.fetchval(
        "INSERT INTO komari_memory_scene_set"
        " (source_path, source_hash, embedding_model, embedding_instruction_hash,"
        "  status) VALUES ($1, $2, $3, $4, $5) RETURNING id",
        "postgresql:seed-integration",
        "seed-src-hash",
        "seed-test-model",
        "seed-test-instruction",
        "BUILDING",
    )
    scenes = await conn.fetch(
        "SELECT id, scene_key, scene_type, content_text, enabled, order_index,"
        " content_hash FROM komari_decision_scenes ORDER BY order_index, id"
    )
    for scene in scenes:
        await conn.execute(
            "INSERT INTO komari_memory_scene_item"
            " (set_id, scene_id, scene_key_snapshot, scene_type_snapshot,"
            "  content_text_snapshot, enabled_snapshot, order_index_snapshot,"
            "  content_hash, embedding, embedding_dim, status)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'READY')",
            set_id,
            scene["id"],
            scene["scene_key"],
            scene["scene_type"],
            scene["content_text"],
            scene["enabled"],
            scene["order_index"],
            scene["content_hash"],
            [0.1, 0.2, 0.3],
            3,
        )
    await conn.execute(
        "UPDATE komari_memory_scene_set SET item_total = $1, item_ready = $1,"
        " item_failed = 0, status = 'READY', ready_at = now() WHERE id = $2",
        len(scenes),
        set_id,
    )
    await conn.execute(
        "INSERT INTO komari_memory_scene_runtime (id, active_set_id)"
        " VALUES (1, $1) ON CONFLICT (id) DO UPDATE"
        " SET active_set_id = EXCLUDED.active_set_id, updated_at = now()",
        set_id,
    )
    service = SceneRuntimeService(
        cast("Any", _ScratchRuntimeRepository(conn)),
    )
    if not await service.load_active_set_cache():
        return None
    return service.get_scene_candidates()


@pytest.mark.skipif(
    not POSTGRES_URL, reason="未设置 KOMARI_TEST_POSTGRES_URL，跳过集成测试"
)
async def test_fresh_database_seed_provides_required_scenes_and_runtime_snapshot() -> None:
    """AC3：全新库播种后含四必需固定场景 + 一般场景，且能构建运行时快照。"""
    if not _same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")

    scratch, scratch_url = await _prepare_head_scratch()
    try:
        result = _run_seed(scratch_url)
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

        conn = await asyncpg.connect(**scratch)
        try:
            fixed_rows = await conn.fetch(
                "SELECT scene_key, scene_type, enabled, content_text"
                " FROM komari_decision_scenes WHERE scene_type = 'fixed'"
            )
            fixed_by_key = {row["scene_key"]: row for row in fixed_rows}
            assert set(fixed_by_key) == set(REQUIRED_FIXED_SCENE_KEYS), (
                f"播种后必需固定场景不齐: {sorted(fixed_by_key)}"
            )
            assert all(row["enabled"] for row in fixed_rows)
            assert all(str(row["content_text"]).strip() for row in fixed_rows)

            general_count = await conn.fetchval(
                "SELECT count(*) FROM komari_decision_scenes"
                " WHERE scene_type = 'general' AND enabled = true"
            )
            assert general_count >= 1, "播种后缺少一般场景"

            snapshot = await _build_runtime_snapshot(conn)
            assert snapshot is not None, "播种后的场景无法构建运行时快照"
            assert set(snapshot.fixed_candidates) == set(
                REQUIRED_FIXED_SCENE_KEYS
            ), "运行时快照固定候选不齐"
            assert len(snapshot.general_candidates) >= 1, "运行时快照缺少一般候选"
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(str(scratch["database"]))


@pytest.mark.skipif(
    not POSTGRES_URL, reason="未设置 KOMARI_TEST_POSTGRES_URL，跳过集成测试"
)
async def test_seed_preserves_existing_scenes_inserts_missing_and_rerun_is_idempotent() -> None:
    """AC4/AC5：只补缺失；已有默认/管理员场景原值不变；重跑不重复不修改。"""
    if not _same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")

    scratch, scratch_url = await _prepare_head_scratch()
    try:
        conn = await asyncpg.connect(**scratch)
        try:
            # 模拟已有部分场景：两个固定场景（其一管理员自定义内容）、
            # 一个 seed 中不存在的管理员自定义一般场景（且被停用）
            await _insert_scene(
                conn,
                scene_key="NOISE",
                scene_type="fixed",
                content_text="管理员自定义的 NOISE 内容",
                content_hash="custom-hash-noise",
                enabled=True,
                order_index=0,
            )
            await _insert_scene(
                conn,
                scene_key="MEANINGFUL",
                scene_type="fixed",
                content_text="既有默认 MEANINGFUL 内容",
                content_hash="existing-hash-meaningful",
                enabled=True,
                order_index=1,
            )
            await _insert_scene(
                conn,
                scene_key="CUSTOM_SCENE",
                scene_type="general",
                content_text="管理员自定义一般场景",
                content_hash="custom-hash-general",
                enabled=False,
                order_index=10,
            )

            result = _run_seed(scratch_url)
            assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

            noise = await conn.fetchrow(
                "SELECT content_text, content_hash, enabled, order_index"
                " FROM komari_decision_scenes WHERE scene_key = 'NOISE'"
            )
            assert noise["content_text"] == "管理员自定义的 NOISE 内容"
            assert noise["content_hash"] == "custom-hash-noise"
            assert noise["enabled"] is True

            meaningful = await conn.fetchrow(
                "SELECT content_text, content_hash"
                " FROM komari_decision_scenes WHERE scene_key = 'MEANINGFUL'"
            )
            assert meaningful["content_text"] == "既有默认 MEANINGFUL 内容"
            assert meaningful["content_hash"] == "existing-hash-meaningful"

            custom = await conn.fetchrow(
                "SELECT content_text, enabled"
                " FROM komari_decision_scenes WHERE scene_key = 'CUSTOM_SCENE'"
            )
            assert custom is not None, "seed 不得删除管理员自定义场景"
            assert custom["content_text"] == "管理员自定义一般场景"
            assert custom["enabled"] is False, "seed 不得改写已有场景的启用状态"

            missing_keys = {
                key
                for key in REQUIRED_FIXED_SCENE_KEYS
                if key not in {"NOISE", "MEANINGFUL"}
            }
            for key in missing_keys:
                row = await conn.fetchrow(
                    "SELECT scene_type, enabled FROM komari_decision_scenes"
                    " WHERE scene_key = $1",
                    key,
                )
                assert row is not None, f"seed 应补插缺失固定场景: {key}"
                assert row["scene_type"] == "fixed"
                assert row["enabled"] is True

            general_count = await conn.fetchval(
                "SELECT count(*) FROM komari_decision_scenes"
                " WHERE scene_type = 'general'"
            )
            assert general_count >= 2, "seed 应补插缺失一般场景且保留自定义场景"

            before = await conn.fetch(
                "SELECT scene_key, scene_type, content_text, content_hash,"
                " enabled, order_index FROM komari_decision_scenes ORDER BY id"
            )

            result = _run_seed(scratch_url)
            assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

            after = await conn.fetch(
                "SELECT scene_key, scene_type, content_text, content_hash,"
                " enabled, order_index FROM komari_decision_scenes ORDER BY id"
            )
            assert [dict(row) for row in before] == [dict(row) for row in after], (
                "重跑不得重复插入或修改已有内容"
            )
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(str(scratch["database"]))


@pytest.mark.skipif(
    not POSTGRES_URL, reason="未设置 KOMARI_TEST_POSTGRES_URL，跳过集成测试"
)
async def test_seed_invalid_file_fails_and_leaves_database_untouched(
    tmp_path: Path,
) -> None:
    """AC6：非法 seed 文件明确失败，且数据库无任何部分写入。"""
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

        conn = await asyncpg.connect(**scratch)
        try:
            count = await conn.fetchval(
                "SELECT count(*) FROM komari_decision_scenes"
            )
            assert count == 0, "非法 seed 不得产生部分写入"
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(str(scratch["database"]))
