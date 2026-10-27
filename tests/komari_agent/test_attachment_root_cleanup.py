"""Parent-owned DSH attachment temporary-root lifecycle."""

from __future__ import annotations

import asyncio
import os
import signal
from typing import TYPE_CHECKING

import pytest

from komari_bot.agent import AgentHost, AgentHostUnavailableError, HostState
from komari_bot.agent import host as host_module
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


async def test_stdout_eof_retires_live_child_before_root_removal(tmp_path: Path) -> None:
    """stdout-only EOF is not proof of process exit; retire the child first."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    script = tmp_path / "stdout-only.mjs"
    script.write_text(
        "import { closeSync, mkdirSync, writeFileSync } from 'node:fs';\n"
        "const rootIndex = process.argv.indexOf('--attachment-root');\n"
        "const root = process.argv[rootIndex + 1];\n"
        "process.on('SIGTERM', () => {});\n"
        "process.stdout.write(JSON.stringify({v:1,kind:'ready',id:'ready',payload:{state:'ready',protocol_version:1,host_version:'probe',bindings:{chat:false,summary:false}}})+'\\n');\n"
        "setTimeout(() => { closeSync(1); setTimeout(() => { mkdirSync(root, { recursive: true }); writeFileSync(root + '/orphan', 'late'); }, 100); }, 100);\n"
        "setInterval(() => {}, 1000);\n",
        encoding="utf-8",
    )
    host = AgentHost(
        runtime_root=tmp_path,
        data_dir=data_dir,
        entry_script=script,
        close_timeout=0.2,
    )
    try:
        await host.start()
        root = host.attachment_root
        assert root is not None and root.is_dir()
        reader_task = host._reader_task
        assert reader_task is not None
        await asyncio.wait_for(asyncio.shield(reader_task), timeout=5)
        assert host.process_id is None, "stdout EOF must retire the live child"
        assert not root.exists()
    finally:
        await host.close()


class _ExhaustedProcess:
    """A child whose bounded terminate/kill attempts all time out."""

    returncode: int | None = None

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        return None

    async def wait(self) -> int:
        await asyncio.sleep(60)
        return 1


async def test_exhausted_termination_withholds_root_until_observed_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "attachments"
    root.mkdir()
    process = _ExhaustedProcess()
    host = AgentHost(
        runtime_root=tmp_path,
        data_dir=tmp_path / "data",
        entry_script=tmp_path / "unused.mjs",
        close_timeout=0.01,
    )
    host.attachment_root = root
    host._attachment_root_cleaned = False
    host._process = process  # type: ignore[assignment]
    monkeypatch.setattr(host_module, "TERMINATE_TIMEOUT", 0.01)

    await host._on_stdout_eof()

    assert root.exists()
    assert host._attachment_root_cleaned is False
    process.returncode = 0
    host._cleanup_attachment_root()
    assert not root.exists()
    assert host._attachment_root_cleaned is True
