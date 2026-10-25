"""TSK-189/TSK-190 版本化初始数据播种 —— 公开契约（无数据库）测试。

本文件定义播种入口的公开契约（测试即规范）：

- 公开 CLI：``python -m komari_bot.db.seed_bootstrap [--seed-file PATH]``
- 默认版本化 seed 文件：``komari_bot/db/initial_data/scenes.yaml``
- 命令先校验 seed 文件、后访问数据库；成功退出码 0，失败以非零退出码
  结束并指明出错的 seed 文件（阻止冷启动）。
- 容器 prestart 在 ``upgrade head`` 之后调用同一命令，``set -e`` 使任一步
  失败即中止容器启动（fail fast）。

TSK-190 起场景初始化退役：默认资产只携带必需 Prompt 资源块，manifest 不再
要求任何场景键（``fixed_candidates`` / ``general_scenes``）。旧场景键按无关
顶层键忽略，校验层不新增拒绝规则；YAML 重复键拒绝仍是通用规则，覆盖根键与
Prompt 映射键。

Prompt 字段名从 ``tests/config/prompt_field_contract.py`` oracle 派生，测试
输入使用自有 marker，不复制生产默认正文。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.config.prompt_field_contract import (
    PROMPT_RESOURCE_IDS,
    find_prompt_mapping,
    prompt_marker_values,
)

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

#: 必需 Prompt 资源块在 manifest 中的节名（只影响可读性，不参与定位）。
_SECTION_BY_RESOURCE: dict[str, str] = {
    "komari_chat": "chat_prompt",
    "group_history_summary": "group_history_summary_prompt",
}


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


def _prompt_manifest_text(*, duplicate_chat_field: str | None = None) -> str:
    """构造测试自有的只带 Prompt 的 manifest 文本（可注入 chat 块重复键）。"""
    lines: list[str] = ['version: "1"']
    for resource_id in PROMPT_RESOURCE_IDS:
        lines.append(f"{_SECTION_BY_RESOURCE[resource_id]}:")
        values = prompt_marker_values(resource_id)
        for field in sorted(values):
            rendered = json.dumps(values[field], ensure_ascii=False)
            lines.append(f"  {field}: {rendered}")
            if duplicate_chat_field is not None and field == duplicate_chat_field:
                duplicate = json.dumps(f"{values[field]}-dup", ensure_ascii=False)
                lines.append(f"  {field}: {duplicate}")
    return "\n".join(lines) + "\n"


def test_versioned_seed_file_carries_prompt_blocks_without_scene_blocks() -> None:
    """默认资产只带必需 Prompt 资源块，不再携带退役场景块。"""
    assert DEFAULT_SEED_FILE.is_file(), (
        f"缺少版本化初始数据文件: {DEFAULT_SEED_FILE}"
    )
    raw = yaml.safe_load(DEFAULT_SEED_FILE.read_text(encoding="utf-8")) or {}
    assert isinstance(raw, dict), "seed 文件根节点必须是对象"
    assert raw.get("version"), "版本化初始数据文件必须携带 version 标记"

    for resource_id in PROMPT_RESOURCE_IDS:
        mapping = find_prompt_mapping(raw, resource_id)
        assert mapping is not None, (
            f"默认资产缺少 {resource_id} Prompt 初始数据块"
        )
        assert all(str(value).strip() for value in mapping.values())

    assert "fixed_candidates" not in raw, "默认资产不得再携带 fixed 场景块"
    assert "general_scenes" not in raw, "默认资产不得再携带一般场景块"


def test_seed_cli_exposes_documented_seed_file_option() -> None:
    """公开 CLI 存在并暴露 --seed-file 选项。"""
    result = _run_seed_cli("--help")
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, output
    assert "--seed-file" in output


def test_seed_cli_validates_seed_file_before_database_access(tmp_path: Path) -> None:
    """非法 seed 在触碰数据库之前即失败并指出出错文件。"""
    bad_file = tmp_path / "bad-root.yaml"
    bad_file.write_text("- just\n- a list\n", encoding="utf-8")
    result = _run_seed_cli(seed_file=bad_file)
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0, output
    assert str(bad_file) in output, "报错必须指明出错的 seed 文件"
    assert "seed-unreachable.invalid" not in output, (
        "seed 校验失败前不得尝试连接数据库"
    )


@pytest.mark.parametrize(
    ("case", "duplicated_key"),
    [("root-version", "version"), ("chat-prompt-field", "system_prompt")],
)
def test_seed_cli_rejects_duplicate_yaml_keys(
    case: str,
    duplicated_key: str,
    tmp_path: Path,
) -> None:
    """YAML 重复键（根键或 Prompt 映射键）明确失败，且不触碰数据库。

    YAML 对 mapping 重复键默认静默覆盖，校验层必须独立检出；否则重复键
    被折叠后会被漏过，命令会继续访问数据库而不是在校验阶段失败。
    """
    body = (
        _prompt_manifest_text() + '\nversion: "2"\n'
        if case == "root-version"
        else _prompt_manifest_text(duplicate_chat_field=duplicated_key)
    )
    seed_file = tmp_path / f"duplicate-key-{case}.yaml"
    seed_file.write_text(body, encoding="utf-8")

    result = _run_seed_cli(seed_file=seed_file)
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0, output
    assert "YAML 解析失败" in output, output
    assert duplicated_key in output, "失败信息必须点名重复的键"
    assert str(seed_file) in output, "报错必须指明出错的 seed 文件"
    assert "seed-unreachable.invalid" not in output, (
        "seed 校验失败前不得尝试连接数据库"
    )


@pytest.mark.parametrize(
    "body",
    [
        "- just\n- a list\n",
        "version: [unclosed\n",
    ],
    ids=["root-list", "malformed-yaml"],
)
def test_seed_cli_rejects_invalid_seed_file_format(
    body: str,
    tmp_path: Path,
) -> None:
    """非法格式（根节点非对象 / YAML 解析失败）明确失败。"""
    seed_file = tmp_path / "invalid-format.yaml"
    seed_file.write_text(body, encoding="utf-8")
    result = _run_seed_cli(seed_file=seed_file)
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0, output
    assert str(seed_file) in output, "报错必须指明出错的 seed 文件"


def test_prestart_runs_upgrade_head_then_seed_bootstrap_with_fail_fast(
    tmp_path: Path,
) -> None:
    """成功路径：prestart 先执行 upgrade head，再调用播种命令。"""
    result, log_path = _run_prestart_with_fake_python(tmp_path)
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, output
    invocations = log_path.read_text(encoding="utf-8").splitlines()
    assert invocations == [MIGRATE_INVOCATION, SEED_INVOCATION], (
        "prestart 必须严格按 upgrade head → seed_bootstrap 顺序各执行一次"
    )


def test_prestart_fails_fast_when_migration_command_fails(tmp_path: Path) -> None:
    """迁移命令失败时 prestart 非零退出，且不调用播种命令。"""
    result, log_path = _run_prestart_with_fake_python(tmp_path, migrate_exit=1)
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0, output
    invocations = log_path.read_text(encoding="utf-8").splitlines()
    assert invocations == [MIGRATE_INVOCATION], (
        "迁移失败时 prestart 必须非零退出且不得调用播种命令（迁移仅调用一次）"
    )


def test_prestart_fails_fast_when_seed_command_fails(tmp_path: Path) -> None:
    """播种失败时 prestart 非零退出；迁移与播种各只调用一次。"""
    result, log_path = _run_prestart_with_fake_python(tmp_path, seed_exit=1)
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0, output
    invocations = log_path.read_text(encoding="utf-8").splitlines()
    assert invocations == [MIGRATE_INVOCATION, SEED_INVOCATION], (
        "播种失败前迁移与播种必须各执行恰好一次"
    )
