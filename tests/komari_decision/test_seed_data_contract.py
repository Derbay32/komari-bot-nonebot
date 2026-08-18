"""TSK-189 版本化初始数据播种 —— 公开契约（无数据库）测试。

本文件定义播种入口的公开契约（测试即规范）：

- 公开 CLI：``python -m komari_bot.db.seed_bootstrap [--seed-file PATH]``
- 默认版本化 seed 文件：``komari_bot/db/initial_data/scenes.yaml``
- 命令先校验 seed 文件、后访问数据库；成功退出码 0，失败以非零退出码
  结束并指明出错的 seed 文件（阻止冷启动）。
- 容器 prestart 在 ``upgrade head`` 之后调用同一命令，``set -e`` 使任一步
  失败即中止容器启动（fail fast）。

必需固定键不导入生产实现，统一使用测试 oracle
``tests/komari_decision/required_fixed_scene_keys.py``。

AC7 的运行时侧（只从 PostgreSQL 构建模板、数据库短暂故障保留最后有效
快照）属于既有行为，本文件以回归钉点覆盖，预期绿灯而非红灯。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from komari_bot.plugins.komari_decision.services.scene_runtime_service import (
    SceneRuntimeService,
)
from komari_bot.plugins.komari_decision.services.scene_template_loader import (
    PostgresSceneTemplateLoader,
)
from tests.komari_decision.required_fixed_scene_keys import REQUIRED_FIXED_SCENE_KEYS

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SEED_FILE = (
    PROJECT_ROOT / "komari_bot" / "db" / "initial_data" / "scenes.yaml"
)
CLI_MODULE = "komari_bot.db.seed_bootstrap"

#: prestart 的公开执行 seam：与 docker/start.sh 实际 source 的是同一脚本。
PRE_START_SCRIPT = PROJECT_ROOT / "docker" / "prestart.sh"
ORM_BOOTSTRAP_MODULE = "komari_bot.db.orm_bootstrap"

#: 假 python 应记录的两次调用（顺序即契约：先迁移，后播种）。
MIGRATE_INVOCATION = f"-m {ORM_BOOTSTRAP_MODULE} upgrade head"
SEED_INVOCATION = f"-m {CLI_MODULE}"

#: 故意不可达的数据库地址：校验类用例必须在该地址被触碰前失败。
UNREACHABLE_DB_URL = (
    "postgresql+asyncpg://seed_test:seed_test@seed-unreachable.invalid:1/seed_test"
)


def _run_seed_cli(
    *args: str,
    seed_file: str | Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """以公开 CLI 形式运行播种命令（可复用于本地/容器/CI 的同一命令）。"""
    env = os.environ.copy()
    env["SQLALCHEMY_DATABASE_URL"] = UNREACHABLE_DB_URL
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    command = [sys.executable, "-m", CLI_MODULE]
    if seed_file is not None:
        command.extend(["--seed-file", str(seed_file)])
    command.extend(args)
    return subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


_FAKE_PYTHON_SCRIPT = """#!/bin/sh
# 测试替身：把每次调用的参数原样追加到 FAKE_PYTHON_LOG，
# 并按被调模块从环境变量读取退出码（缺省 0）。
printf '%s\\n' "$*" >> "$FAKE_PYTHON_LOG"
case "$*" in
  *komari_bot.db.seed_bootstrap*)
    exit "${FAKE_PYTHON_SEED_EXIT:-0}"
    ;;
esac
exit "${FAKE_PYTHON_MIGRATE_EXIT:-0}"
"""


def _run_prestart_with_fake_python(
    tmp_path: Path,
    *,
    migrate_exit: int = 0,
    seed_exit: int = 0,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """通过公开执行 seam 运行 prestart，并用受控假 python 记录调用。

    PATH 前置只含一个假 ``python`` 可执行文件：它把每次调用的 ``-m ...``
    参数原样追加到日志文件，并按所调模块返回测试指定的退出码。
    返回 ``(进程结果, 调用日志路径)``；断言全部基于外部可观察行为
    （真实脚本退出码 + 实际发生的 python 调用序列），不解析脚本文本。
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_python = bin_dir / "python"
    fake_python.write_text(_FAKE_PYTHON_SCRIPT, encoding="utf-8")
    fake_python.chmod(0o755)

    log_path = tmp_path / "python-invocations.log"
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["FAKE_PYTHON_LOG"] = str(log_path)
    env["FAKE_PYTHON_MIGRATE_EXIT"] = str(migrate_exit)
    env["FAKE_PYTHON_SEED_EXIT"] = str(seed_exit)

    result = subprocess.run(
        ["/bin/sh", str(PRE_START_SCRIPT)],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    return result, log_path


def _scene_seed_text(
    *,
    fixed_keys: tuple[str, ...] = REQUIRED_FIXED_SCENE_KEYS,
    general_scenes: list[dict[str, str]] | None = None,
) -> str:
    """构造测试自有的 seed 文件内容（键来自 oracle，值为测试字面量）。"""
    if general_scenes is None:
        general_scenes = [{"id": "scene_test_general", "text": "测试一般场景内容"}]
    lines = ['version: "1"', "fixed_candidates:"]
    lines.extend(f'  {key}: "固定内容 {key}"' for key in fixed_keys)
    lines.append("general_scenes:")
    for scene in general_scenes:
        lines.append(f'  - id: "{scene["id"]}"')
        lines.append(f'    text: "{scene["text"]}"')
    return "\n".join(lines) + "\n"


class _RowsOnlyRepository:
    """只实现 PostgresSceneTemplateLoader 依赖的两个接口。"""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def list_scenes(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        assert enabled_only is True
        return [dict(row) for row in self.rows]

    @staticmethod
    def compute_scene_source_hash(rows: list[dict[str, Any]]) -> str:
        del rows
        return "row-derived-source-hash"


def _runtime_rows() -> list[dict[str, Any]]:
    rows = [
        {
            "id": index,
            "scene_key": scene_key,
            "scene_type": "fixed",
            "content_text": f"内容 {scene_key}",
            "enabled": True,
            "order_index": index,
            "content_hash": f"hash-{scene_key}",
        }
        for index, scene_key in enumerate(REQUIRED_FIXED_SCENE_KEYS, start=1)
    ]
    rows.append(
        {
            "id": 5,
            "scene_key": "GREETING",
            "scene_type": "general",
            "content_text": "问候场景",
            "enabled": True,
            "order_index": 5,
            "content_hash": "hash-GREETING",
        }
    )
    return rows


def _snapshot_items(tag: str) -> list[dict[str, Any]]:
    items = [
        {
            "scene_key": scene_key,
            "scene_type": "fixed",
            "content_text": f"{tag} {scene_key}",
            "embedding": [0.1 * (index + 1), 0.1 * (index + 1)],
            "order_index": index,
            "status": "READY",
            "enabled": True,
        }
        for index, scene_key in enumerate(REQUIRED_FIXED_SCENE_KEYS)
    ]
    items.append(
        {
            "scene_key": f"SCENE_{tag.upper()}",
            "scene_type": "general",
            "content_text": f"{tag} general",
            "embedding": [0.5, 0.5],
            "order_index": 4,
            "status": "READY",
            "enabled": True,
        }
    )
    return items


class _FlakyRepository:
    """先成功后故障的仓储桩：验证数据库短暂故障时最后有效快照语义。"""

    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.items = items
        self.fail = False
        self.get_active_calls = 0

    async def get_active_set(self) -> dict[str, Any] | None:
        self.get_active_calls += 1
        if self.fail:
            raise RuntimeError("数据库暂不可用（测试模拟）")
        return {
            "id": 1,
            "status": "READY",
            "runtime_updated_at": f"ts-{self.get_active_calls}",
        }

    async def list_items_by_set(
        self,
        set_id: int,
        status: str | None = None,
        *,
        enabled_only: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        del set_id, status, enabled_only, limit
        if self.fail:
            raise RuntimeError("数据库暂不可用（测试模拟）")
        return [dict(item) for item in self.items]


def test_versioned_scene_seed_file_is_present_and_well_formed() -> None:
    """AC1：版本化场景初始数据存在且形状合法。"""
    assert DEFAULT_SEED_FILE.is_file(), (
        f"缺少版本化场景初始数据文件: {DEFAULT_SEED_FILE}"
    )
    raw = yaml.safe_load(DEFAULT_SEED_FILE.read_text(encoding="utf-8")) or {}
    assert isinstance(raw, dict), "seed 文件根节点必须是对象"
    assert raw.get("version"), "版本化初始数据文件必须携带 version 标记"

    fixed_candidates = raw.get("fixed_candidates")
    assert isinstance(fixed_candidates, dict), "fixed_candidates 必须是对象"
    missing = [
        key for key in REQUIRED_FIXED_SCENE_KEYS if key not in fixed_candidates
    ]
    assert missing == [], f"seed 文件缺少必需 fixed 场景: {missing}"
    assert all(str(value).strip() for value in fixed_candidates.values())

    general_scenes = raw.get("general_scenes")
    assert isinstance(general_scenes, list) and general_scenes, (
        "seed 文件必须包含至少一个 general scene"
    )
    for item in general_scenes:
        assert isinstance(item, dict)
        assert str(item.get("id", "")).strip(), "general scene 缺少 id"
        assert str(item.get("text", "")).strip(), "general scene 缺少 text"


def test_seed_cli_exposes_documented_seed_file_option() -> None:
    """AC1：公开 CLI 存在并暴露 --seed-file 选项。"""
    result = _run_seed_cli("--help")
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, output
    assert "--seed-file" in output


def test_seed_cli_validates_seed_file_before_database_access(tmp_path: Path) -> None:
    """AC6：非法 seed 在触碰数据库之前即失败并指出出错文件。"""
    bad_file = tmp_path / "bad-root.yaml"
    bad_file.write_text("- just\n- a list\n", encoding="utf-8")
    result = _run_seed_cli(seed_file=bad_file)
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0, output
    assert str(bad_file) in output, "报错必须指明出错的 seed 文件"
    assert "seed-unreachable.invalid" not in output, (
        "seed 校验失败前不得尝试连接数据库"
    )


@pytest.mark.parametrize("missing_key", REQUIRED_FIXED_SCENE_KEYS)
def test_seed_cli_rejects_seed_missing_required_fixed_scene(
    missing_key: str,
    tmp_path: Path,
) -> None:
    """AC6：缺任一必需固定场景的 seed 文件明确失败。"""
    fixed_keys = tuple(
        key for key in REQUIRED_FIXED_SCENE_KEYS if key != missing_key
    )
    seed_file = tmp_path / "missing-fixed.yaml"
    seed_file.write_text(_scene_seed_text(fixed_keys=fixed_keys), encoding="utf-8")
    result = _run_seed_cli(seed_file=seed_file)
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0, output
    assert missing_key in output, "失败信息必须点名缺失的固定场景键"
    assert str(seed_file) in output


def test_seed_cli_rejects_seed_without_general_scenes(tmp_path: Path) -> None:
    """AC6：一般场景为空的 seed 文件明确失败。"""
    seed_file = tmp_path / "no-general.yaml"
    seed_file.write_text(_scene_seed_text(general_scenes=[]), encoding="utf-8")
    result = _run_seed_cli(seed_file=seed_file)
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0, output
    assert "general" in output.lower(), "失败信息必须指向一般场景缺失"
    assert str(seed_file) in output


@pytest.mark.parametrize(
    ("general_scenes", "duplicated_key"),
    [
        (
            [{"id": "NOISE", "text": "与 fixed 候选重复的 general key"}],
            "NOISE",
        ),
        (
            [
                {"id": "scene_dup", "text": "第一个"},
                {"id": "scene_dup", "text": "第二个"},
            ],
            "scene_dup",
        ),
    ],
    ids=["fixed-general-duplicate", "within-general-duplicate"],
)
def test_seed_cli_rejects_duplicate_scene_keys(
    general_scenes: list[dict[str, str]],
    duplicated_key: str,
    tmp_path: Path,
) -> None:
    """AC6：seed 文件内重复 scene_key 明确失败。"""
    seed_file = tmp_path / "duplicate-keys.yaml"
    seed_file.write_text(
        _scene_seed_text(general_scenes=general_scenes),
        encoding="utf-8",
    )
    result = _run_seed_cli(seed_file=seed_file)
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0, output
    assert duplicated_key in output, "失败信息必须点名重复的键"
    assert str(seed_file) in output


@pytest.mark.parametrize(
    "body",
    [
        "- just\n- a list\n",
        "fixed_candidates: [unclosed\n",
    ],
    ids=["root-list", "malformed-yaml"],
)
def test_seed_cli_rejects_invalid_seed_file_format(
    body: str,
    tmp_path: Path,
) -> None:
    """AC6：非法格式（根节点非对象 / YAML 解析失败）明确失败。"""
    seed_file = tmp_path / "invalid-format.yaml"
    seed_file.write_text(body, encoding="utf-8")
    result = _run_seed_cli(seed_file=seed_file)
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0, output
    assert str(seed_file) in output, "报错必须指明出错的 seed 文件"


def test_prestart_runs_upgrade_head_then_seed_bootstrap_with_fail_fast(
    tmp_path: Path,
) -> None:
    """AC2 成功路径：prestart 先执行 upgrade head，再调用播种命令。"""
    result, log_path = _run_prestart_with_fake_python(tmp_path)
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, output
    invocations = log_path.read_text(encoding="utf-8").splitlines()
    assert invocations == [MIGRATE_INVOCATION, SEED_INVOCATION], (
        "prestart 必须严格按 upgrade head → seed_bootstrap 顺序各执行一次"
    )


def test_prestart_fails_fast_when_migration_command_fails(tmp_path: Path) -> None:
    """AC2：迁移命令失败时 prestart 非零退出，且不调用播种命令。"""
    result, log_path = _run_prestart_with_fake_python(tmp_path, migrate_exit=1)
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0, output
    invocations = log_path.read_text(encoding="utf-8").splitlines()
    assert invocations == [MIGRATE_INVOCATION], (
        "迁移失败时 prestart 必须非零退出且不得调用播种命令（迁移仅调用一次）"
    )


def test_prestart_fails_fast_when_seed_command_fails(tmp_path: Path) -> None:
    """AC2：播种失败时 prestart 非零退出；迁移与播种各只调用一次。"""
    result, log_path = _run_prestart_with_fake_python(tmp_path, seed_exit=1)
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0, output
    invocations = log_path.read_text(encoding="utf-8").splitlines()
    assert invocations == [MIGRATE_INVOCATION, SEED_INVOCATION], (
        "播种失败前迁移与播种必须各执行恰好一次"
    )


def test_runtime_scene_loader_derives_payload_from_postgresql_rows_only() -> None:
    """AC7：运行时场景模板只从 PostgreSQL 行构建，不读取 seed 文件。"""
    loader = PostgresSceneTemplateLoader(
        cast("Any", _RowsOnlyRepository(_runtime_rows())),
    )

    payload = asyncio.run(loader.load_scene_template())

    assert payload.source_path.endswith((".yaml", ".yml")) is False
    assert payload.source_hash == "row-derived-source-hash"
    assert set(payload.fixed_candidates) == set(REQUIRED_FIXED_SCENE_KEYS)
    assert payload.general_scenes == [{"id": "GREETING", "text": "问候场景"}]


def test_runtime_keeps_last_valid_snapshot_on_transient_db_failure() -> None:
    """AC7：数据库短暂故障时继续使用最后确认有效的内存快照。"""
    repository = _FlakyRepository(_snapshot_items("v1"))
    service = SceneRuntimeService(cast("Any", repository))

    assert asyncio.run(service.load_active_set_cache()) is True
    snapshot = service.get_scene_candidates()
    assert snapshot is not None

    repository.fail = True
    with pytest.raises(RuntimeError):
        asyncio.run(service.refresh_if_runtime_updated())

    assert service.get_scene_candidates() is snapshot
