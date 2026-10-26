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

from nonebot.plugin import _managers, get_loaded_plugins, get_plugin
from nonebot.plugin.manager import PluginManager

# Register the real local plugin search path without importing any plugin.
manager = PluginManager(search_path=["komari_bot/plugins"])
_managers.append(manager)

assert "group_history_summary" in manager.searched_plugins
assert "character_binding" in manager.searched_plugins
assert get_plugin("character_binding") is None
assert get_plugin("group_history_summary") is None
assert not {plugin.name for plugin in get_loaded_plugins()}

# Load summary first; its own require("character_binding") must resolve through
# the actual NoneBot manager and load the real dependency.
plugin = manager.load_plugin("group_history_summary")
assert plugin is not None
assert get_plugin("group_history_summary") is not None
assert get_plugin("character_binding") is not None
assert {p.name for p in get_loaded_plugins()} >= {"group_history_summary", "character_binding"}

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
