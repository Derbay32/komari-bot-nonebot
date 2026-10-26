"""Parent-owned DSH attachment temporary-root lifecycle."""

from __future__ import annotations

import asyncio
import os
import signal
from typing import TYPE_CHECKING

import pytest

from komari_bot.agent import AgentHost, AgentHostUnavailableError, HostState
from tests.komari_agent.dsh_test_support import (
    TEST_HOST_SCRIPT,
    ToolCallResponse,
    host_for,
    running_dsh,
    write_profile,
)


async def _wait_until_removed(path: "Path", max_wait: float = 5.0) -> None:
    async with asyncio.timeout(max_wait):
        while path.exists():  # noqa: ASYNC110
            await asyncio.sleep(0.02)


async def test_unexpected_node_death_removes_the_parent_owned_root(tmp_path: Path) -> None:
    async with running_dsh(
        tmp_path,
        responses=(ToolCallResponse("fixture_complete", {"value": "ok"}),),
    ) as (host, _server):
        root = host.attachment_root
        assert root is not None and root.is_dir()
        pid = host.process_id
        assert pid is not None
        os.kill(pid, signal.SIGKILL)
        await _wait_until_removed(root)
        assert host.state is HostState.UNAVAILABLE


async def test_normal_close_removes_the_parent_owned_root(tmp_path: Path) -> None:
    server_data = tmp_path / "data"
    write_profile(server_data, base_url="http://127.0.0.1:1/v1")
    host = host_for(server_data, entry_script=TEST_HOST_SCRIPT)
    await host.start()
    root = host.attachment_root
    assert root is not None and root.is_dir()
    await host.close()
    assert not root.exists()
    assert host.attachment_root == root


async def test_startup_failure_removes_the_parent_owned_root(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    slow_entry = tmp_path / "slow.mjs"
    slow_entry.write_text("setTimeout(() => {}, 60000)\n", encoding="utf-8")
    host = AgentHost(
        runtime_root=tmp_path,
        data_dir=data_dir,
        entry_script=slow_entry,
        startup_timeout=0.3,
        close_timeout=1.0,
    )
    with pytest.raises(AgentHostUnavailableError):
        await host.start()
    root = host.attachment_root
    assert root is not None
    await _wait_until_removed(root)
    await host.close()


if TYPE_CHECKING:
    from pathlib import Path
