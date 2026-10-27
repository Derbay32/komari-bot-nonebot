"""容器启动契约：先迁移后可选播种，进程异常仍中止启动。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRE_START_SCRIPT = PROJECT_ROOT / "docker" / "prestart.sh"
MIGRATE_INVOCATION = "-m komari_bot.db.orm_bootstrap upgrade head"
SEED_INVOCATION = "-m komari_bot.db.seed_bootstrap"

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
