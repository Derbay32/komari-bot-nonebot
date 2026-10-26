"""Cold loading evidence for the summary plugin dependency declaration."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]

_SCRIPT = r"""
from komari_bot.core.nonebot_compat import install_nonebot_forwardref_compatibility

install_nonebot_forwardref_compatibility()

import nonebot

nonebot.init(_env_file=None, driver="~fastapi", log_level="ERROR")
nonebot.load_plugins("komari_bot/plugins")

loaded = {plugin.name for plugin in nonebot.get_loaded_plugins()}
assert "character_binding" in loaded, loaded
assert "group_history_summary" in loaded, loaded

from komari_bot.plugins.group_history_summary import history_tools

assert history_tools.character_binding.__name__ == "komari_bot.plugins.character_binding"
print("summary-plugin-load-order-ok")
"""


def test_summary_plugin_cold_loads_with_real_dependency_registration() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr
    assert "summary-plugin-load-order-ok" in result.stdout
