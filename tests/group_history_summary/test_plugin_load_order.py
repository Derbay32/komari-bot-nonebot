"""Cold loading and real ASGI startup evidence for the summary plugin."""

from __future__ import annotations

import os
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

_STARTUP_SCRIPT = r"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from komari_bot.core.nonebot_compat import install_nonebot_forwardref_compatibility

install_nonebot_forwardref_compatibility()

import nonebot

os.environ["NO_PROXY"] = "127.0.0.1,localhost"
nonebot.init(_env_file=None, driver="~fastapi", log_level="ERROR")

from aiohttp import web
from aiohttp.test_utils import TestServer
from async_asgi_testclient import TestClient
from nonebot import get_driver
from nonebot.plugin import _managers
from nonebot.plugin.manager import PluginManager

manager = PluginManager(search_path=["komari_bot/plugins"])
_managers.append(manager)
plugin = manager.load_plugin("group_history_summary")
assert plugin is not None

lifespan = get_driver()._lifespan
startup_hooks = [
    hook
    for hook in lifespan._startup_funcs
    if hook.__module__ == "komari_bot.plugins.group_history_summary"
]
shutdown_hooks = [
    hook
    for hook in lifespan._shutdown_funcs
    if hook.__module__ == "komari_bot.plugins.group_history_summary"
]
assert len(startup_hooks) == 1, startup_hooks
assert len(shutdown_hooks) == 1, shutdown_hooks

from komari_bot.plugins.group_history_summary import font_asset

MODE = os.environ["TSK209_STARTUP_MODE"]
FONT_BYTES = b"asgi-startup-font-payload"


async def _main() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        target = Path(temp_dir) / "data" / "fonts" / "LXGWWenKaiGBScreen.ttf"
        font_asset.FONT_PATH = target

        requests = []

        async def _font_handler(_request: web.Request) -> web.Response:
            requests.append("font")
            if MODE == "failure":
                return web.Response(status=503, text="controlled failure")
            return web.Response(body=FONT_BYTES)

        app = web.Application()
        app.router.add_get("/LXGWWenKaiGBScreen.ttf", _font_handler)
        server = TestServer(app)
        await server.start_server()
        font_asset.FONT_URL = str(server.make_url("/LXGWWenKaiGBScreen.ttf"))

        marker_calls = []

        async def _next_startup_marker() -> None:
            marker_calls.append("startup")

        # Keep the registered production hook and put a marker after it. Other
        # plugins' hooks need PostgreSQL/Redis and are outside this startup seam.
        lifespan._startup_funcs[:] = [startup_hooks[0], _next_startup_marker]
        lifespan._shutdown_funcs[:] = []

        try:
            asgi_app = nonebot.get_asgi()
            if MODE == "success":
                async with TestClient(asgi_app):
                    pass
                assert marker_calls == ["startup"], marker_calls
                assert target.read_bytes() == FONT_BYTES
                assert requests == ["font"], requests
                assert list(target.parent.glob(f".{target.name}.*.tmp")) == []

                # A real restart enters the ASGI lifespan again. The existing
                # target must be reused without another HTTP request.
                async with TestClient(asgi_app):
                    pass
                assert marker_calls == ["startup", "startup"], marker_calls
                assert requests == ["font"], requests
                print("summary-startup-success-ok")
            elif MODE == "failure":
                try:
                    async with TestClient(asgi_app):
                        pass
                except Exception as exc:
                    print(f"summary-startup-failure={type(exc).__name__}")
                else:
                    raise AssertionError("ASGI startup unexpectedly succeeded")
                assert marker_calls == [], marker_calls
                assert not target.exists()
                assert list(target.parent.glob(f".{target.name}.*.tmp")) == []
                assert requests == ["font"], requests
                # Starlette reports startup failure without entering its
                # shutdown branch; clean up the still-open NoneBot task group.
                if lifespan._task_group is not None:
                    await lifespan.shutdown()
                print("summary-startup-failure-ok")
            else:
                raise AssertionError(f"unknown mode: {MODE}")
        finally:
            await server.close()


asyncio.run(_main())
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


def test_real_asgi_startup_prepares_missing_font_and_reaches_next_hook() -> None:
    result = _run_startup_case("success")
    assert result.returncode == 0, result.stderr
    assert "summary-startup-success-ok" in result.stdout


def test_real_asgi_startup_failure_stops_before_next_hook() -> None:
    result = _run_startup_case("failure")
    assert result.returncode == 0, result.stderr
    assert "summary-startup-failure-ok" in result.stdout


def _run_startup_case(mode: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["TSK209_STARTUP_MODE"] = mode
    return subprocess.run(
        [sys.executable, "-c", _STARTUP_SCRIPT],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
        env=env,
    )
