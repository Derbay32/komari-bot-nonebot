"""TSK-271 模型加载副作用与迁移链验收。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests.character_binding.conftest import POSTGRES_URL, require_postgres

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_orm_model_loader_does_not_initialize_nonebot_or_database() -> None:
    """Alembic 的源文件模型扫描不能触发插件入口或连接池。"""
    script = """
import sys

from komari_bot.config.typed_config import load_all_plugin_orm_models

loaded = load_all_plugin_orm_models()
assert loaded > 0
assert "nonebot" not in sys.modules
assert "nonebot_plugin_orm" not in sys.modules
assert "komari_bot.plugins.character_binding" not in sys.modules
assert "komari_bot.plugins.character_binding.orm_models" in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(PROJECT_ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_upgrade_head_and_orm_check_pass_on_real_postgres() -> None:
    """迁移链必须在真实测试库完成 upgrade 与 schema check。"""
    require_postgres()
    environment = os.environ.copy()
    environment["SQLALCHEMY_DATABASE_URL"] = POSTGRES_URL
    environment["KOMARI_TEST_POSTGRES_URL"] = POSTGRES_URL

    for command in ("upgrade", "check"):
        args = [sys.executable, "-m", "komari_bot.db.orm_bootstrap", command]
        if command == "upgrade":
            args.append("head")
        result = subprocess.run(
            args,
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (
            f"orm_bootstrap {command} 失败:\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
