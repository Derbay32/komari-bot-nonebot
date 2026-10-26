"""The NoneBot plugin exposes one lazy host and starts it only on driver startup."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]

_SCRIPT = """
import nonebot

nonebot.init(_env_file=None, driver="~fastapi", log_level="ERROR")
import komari_bot.plugins.komari_agent as plugin

host = plugin.get_agent_host()
assert host is plugin.get_agent_host()
assert host.process_id is None
assert host.readiness.model_configured is False
assert "AgentHost" in plugin.__all__
assert "get_agent_host" in plugin.__all__
print("plugin-lifecycle-ok")
"""


def test_plugin_public_host_is_lazy_and_singleton() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "plugin-lifecycle-ok" in result.stdout
