"""Observe bounded control calls through the real child-process pipe."""

from __future__ import annotations

import asyncio
import os
import signal
from typing import TYPE_CHECKING

import pytest

from komari_bot.agent import AgentHostUnavailableError, ControlOperation, HostState
from tests.komari_agent.dsh_test_support import host_for

if TYPE_CHECKING:
    from pathlib import Path


async def test_control_timeout_includes_backpressured_pipe(tmp_path: Path) -> None:
    """A paused owned child cannot make the management deadline unbounded."""
    host = host_for(tmp_path / "data")
    await host.start()
    pid = host.process_id
    assert pid is not None
    os.kill(pid, signal.SIGSTOP)
    try:
        # Each payload fits the protocol. Together these concurrent calls
        # exceed the pipe and asyncio high-water marks, exercising both drain
        # backpressure and queued writers without patching host internals.
        async def bounded_call() -> None:
            with pytest.raises(AgentHostUnavailableError) as caught:
                await host.control(
                    ControlOperation.PROVIDERS_LIST,
                    {"padding": "x" * 60_000},
                    wait_seconds=0.05,
                )
            assert caught.value.code == "control_timeout"

        await asyncio.wait_for(
            asyncio.gather(*(bounded_call() for _ in range(6))),
            timeout=1.0,
        )
    finally:
        os.kill(pid, signal.SIGCONT)
        await host.close()


@pytest.mark.parametrize("cancel", [False, True], ids=["timeout", "cancellation"])
async def test_late_control_reply_does_not_retire_host(
    tmp_path: Path, *, cancel: bool,
) -> None:
    """A real late reply is discarded while the next control call succeeds."""
    host = host_for(tmp_path / "data")
    await host.start()
    pid = host.process_id
    assert pid is not None
    try:
        os.kill(pid, signal.SIGSTOP)
        try:
            if cancel:
                pending = asyncio.create_task(
                    host.control(ControlOperation.PROVIDERS_LIST, {}),
                )
                # Let the small request enter the pipe before cancelling its caller.
                await asyncio.sleep(0)
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            else:
                with pytest.raises(AgentHostUnavailableError) as caught:
                    await host.control(
                        ControlOperation.PROVIDERS_LIST, {}, wait_seconds=0.05,
                    )
                assert caught.value.code == "control_timeout"
        finally:
            os.kill(pid, signal.SIGCONT)
        result = await host.control(ControlOperation.PROVIDERS_LIST, {})
        assert result.value["items"] == []
        assert host.state is HostState.READY
    finally:
        await host.close()
