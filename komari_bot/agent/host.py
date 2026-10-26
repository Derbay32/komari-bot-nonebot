"""Async owner of the single long-lived DSH Node child process."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from komari_bot.core.project_paths import DATA_DIR, PROJECT_ROOT

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

from .protocol import (
    MAX_FRAME_BYTES,
    CallbackFrame,
    ReadyFrame,
    ResultFrame,
    ServerFrame,
    decode_server_frame,
    encode_callback_result_error,
    encode_callback_result_success,
    encode_cancel,
    encode_request,
    encode_shutdown,
    read_frame,
)
from .types import (
    MAX_CALLBACK_DATA_BYTES,
    AgentCallbackHandler,
    AgentCallbackRequest,
    AgentHostUnavailableError,
    AgentProtocolError,
    AgentTaskError,
    AgentTaskRequest,
    AgentTaskResult,
    HostReadiness,
    HostState,
    JsonValue,
    ReadinessState,
    TaskKind,
    json_payload,
    validate_json_value,
)

DEFAULT_STARTUP_TIMEOUT: Final = 30.0
DEFAULT_CLOSE_TIMEOUT: Final = 10.0
CANCEL_DRAIN_TIMEOUT: Final = 5.0
TERMINATE_TIMEOUT: Final = 2.0
_MAX_TOMBSTONES: Final = 256
_LOG_LINE_LIMIT: Final = 2000

_logger = logging.getLogger("komari_agent.host")

_UNAVAILABLE_BINDINGS: Final[Mapping[TaskKind, bool]] = {
    TaskKind.CHAT: False,
    TaskKind.SUMMARY: False,
}


@dataclass
class _RequestState:
    request: AgentTaskRequest
    callbacks: Mapping[str, AgentCallbackHandler]
    future: asyncio.Future[Mapping[str, JsonValue]]
    drain_future: asyncio.Future[None] | None = None
    callback_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    cancelled: bool = False
    request_sent: bool = False
    terminal: bool = False


class AgentHost:
    """One process-wide DSH host with async start/execute/close and readiness.

    The child is spawned with private pipes only. It is never restarted after a
    terminal exit, and this class never opens an ORM or Redis resource.
    """

    def __init__(
        self,
        *,
        runtime_root: Path | None = None,
        data_dir: Path | None = None,
        node_executable: str | None = None,
        entry_script: Path | None = None,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
        close_timeout: float = DEFAULT_CLOSE_TIMEOUT,
        log: logging.Logger | None = None,
    ) -> None:
        root = (runtime_root or PROJECT_ROOT / "dsh").resolve()
        self.runtime_root = root
        self.data_dir = (data_dir or DATA_DIR).resolve()
        self.profile_dir = self.data_dir / "dsh"
        self.dsh_home = self.profile_dir
        self.node_executable = node_executable
        self.entry_script = entry_script or root / "runtime" / "dist" / "main.js"
        self.startup_timeout = startup_timeout
        self.close_timeout = close_timeout
        self._log = log or _logger
        self._lifecycle_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._state = HostState.NEW
        self._readiness = HostReadiness(state=ReadinessState.UNAVAILABLE, bindings=_UNAVAILABLE_BINDINGS)
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._ready_future: asyncio.Future[ReadyFrame] | None = None
        self._requests: dict[str, _RequestState] = {}
        self._all_callback_tasks: set[asyncio.Task[None]] = set()
        self._detached_sends: set[asyncio.Task[None]] = set()
        self._tombstones: deque[str] = deque()
        self._tombstone_set: set[str] = set()
        self._exit_handled = False

    @property
    def state(self) -> HostState:
        """Raw lifecycle state."""
        return self._state

    @property
    def readiness(self) -> HostReadiness:
        """Public readiness without implying a model is configured."""
        return self._readiness

    @property
    def process_id(self) -> int | None:
        """Child PID while it is running (test and operational diagnostics)."""
        process = self._process
        return None if process is None or process.returncode is not None else process.pid

    async def start(self) -> None:
        """Spawn the child and await its one readiness announcement."""
        async with self._lifecycle_lock:
            if self._state is HostState.READY:
                return
            if self._state is HostState.NEW:
                pass
            elif self._state is HostState.UNAVAILABLE:
                raise AgentHostUnavailableError("host_unavailable")
            else:
                raise AgentHostUnavailableError("host_closed")

            self._state = HostState.STARTING
            node = self.node_executable or shutil.which("node")
            if node is None:
                await self._startup_failed("node_missing")
            if not self.entry_script.is_file():
                await self._startup_failed("entry_missing")

            assert node is not None
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            environment = dict(os.environ)
            environment["DSH_HOME"] = str(self.dsh_home)
            environment["DSH_TELEMETRY_DISABLED"] = "1"
            self._ready_future = asyncio.get_running_loop().create_future()
            try:
                self._process = await asyncio.create_subprocess_exec(
                    node,
                    str(self.entry_script),
                    "--profile-dir",
                    str(self.profile_dir),
                    "--install-anchor",
                    str(self.runtime_root / "package.json"),
                    "--base-config",
                    str(self.runtime_root / "compose" / "cordis.yml"),
                    "--dsh-home",
                    str(self.dsh_home),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(self.runtime_root),
                    env=environment,
                    limit=MAX_FRAME_BYTES,
                )
            except OSError as exc:
                raise AgentHostUnavailableError("spawn_failed") from exc

            self._reader_task = asyncio.create_task(self._reader_loop(), name="komari-agent-reader")
            self._stderr_task = asyncio.create_task(self._stderr_loop(), name="komari-agent-stderr")
            ready = self._ready_future
            try:
                frame = await asyncio.wait_for(asyncio.shield(ready), timeout=self.startup_timeout)
            except TimeoutError as exc:
                if not ready.done():
                    ready.cancel()
                await self._abort_process()
                await self._mark_unavailable("startup_timeout")
                raise AgentHostUnavailableError("startup_timeout") from exc
            except AgentHostUnavailableError:
                await self._mark_unavailable("process_exited")
                raise
            if frame.state != "ready":
                await self._abort_process()
                await self._mark_unavailable(frame.reason or "host_unavailable")
                raise AgentHostUnavailableError(frame.reason or "host_unavailable")
            self._state = HostState.READY
            all_configured = all(frame.bindings.values())
            self._readiness = HostReadiness(
                state=ReadinessState.READY if all_configured else ReadinessState.UNCONFIGURED_READY,
                bindings=dict(frame.bindings),
            )

    async def execute(
        self,
        request: AgentTaskRequest,
        callbacks: Mapping[str, AgentCallbackHandler],
    ) -> AgentTaskResult:
        """Run one task and return only its validated business-result slot."""
        if self._state is not HostState.READY:
            raise AgentHostUnavailableError("host_unavailable")
        if "admission" not in callbacks:
            raise AgentTaskError("admission_handler_missing")

        request_id = uuid.uuid4().hex
        future: asyncio.Future[Mapping[str, JsonValue]] = asyncio.get_running_loop().create_future()
        state = _RequestState(request=request, callbacks=dict(callbacks), future=future)
        self._requests[request_id] = state
        primary_error: BaseException | None = None
        try:
            await self._send(encode_request(request_id, request))
            state.request_sent = True
            result = await future
            return AgentTaskResult(task_id=request.task_id, kind=request.kind, result=result)
        except asyncio.CancelledError as exc:
            primary_error = exc
            await self._cancel_request(request_id, state)
            raise
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            self._requests.pop(request_id, None)
            await self._cancel_callback_tasks(state)
            if primary_error is not None and not future.done():
                future.cancel()

    async def close(self) -> None:
        """Cancel own tasks, drain boundedly, then terminate/kill only if needed."""
        async with self._lifecycle_lock:
            if self._state is HostState.CLOSED:
                return
            was_new = self._state is HostState.NEW
            self._state = HostState.CLOSING
            for request_id, state in list(self._requests.items()):
                state.cancelled = True
                await self._cancel_callback_tasks(state)
                if state.request_sent:
                    with contextlib.suppress(AgentHostUnavailableError, AgentProtocolError):
                        await self._send(encode_cancel(request_id))
            if not was_new:
                with contextlib.suppress(AgentHostUnavailableError, AgentProtocolError):
                    await self._send(encode_shutdown())
            process = self._process
            if process is not None and process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(), timeout=self.close_timeout)
                except TimeoutError:
                    await self._terminate_process(process)
            await self._stop_reader_and_stderr()
            self._fail_pending("host_closed")
            await self._finish_callback_tasks()
            self._process = None
            self._state = HostState.CLOSED
            self._readiness = HostReadiness(state=ReadinessState.UNAVAILABLE, bindings=_UNAVAILABLE_BINDINGS)

    async def _startup_failed(self, code: str) -> None:
        await self._mark_unavailable(code)
        raise AgentHostUnavailableError(code)

    async def _reader_loop(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            await self._mark_unavailable("process_exited")
            return
        try:
            while True:
                line = await read_frame(process.stdout)
                if line is None:
                    break
                self._dispatch(decode_server_frame(line))
        except asyncio.CancelledError:
            raise
        except AgentProtocolError as exc:
            await self._protocol_failed(exc.code)
            return
        except Exception:
            await self._protocol_failed("reader_failed")
            return
        await self._on_process_exit("process_exited")

    async def _stderr_loop(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if len(text) > _LOG_LINE_LIMIT:
                    text = f"{text[:_LOG_LINE_LIMIT]}…"
                self._log.info("[komari_agent.dsh] %s", text)
        except asyncio.CancelledError:
            raise
        except (ValueError, OSError):
            self._log.info("[komari_agent.dsh] stderr closed")

    def _dispatch(self, frame: ServerFrame) -> None:
        match frame:
            case ReadyFrame():
                self._dispatch_ready(frame)
            case ResultFrame():
                self._dispatch_result(frame)
            case CallbackFrame():
                self._spawn_callback(frame)

    def _dispatch_ready(self, frame: ReadyFrame) -> None:
        future = self._ready_future
        if future is None or future.done():
            raise AgentProtocolError("duplicate_ready")
        future.set_result(frame)

    def _dispatch_result(self, frame: ResultFrame) -> None:
        state = self._requests.get(frame.request_id)
        if state is None:
            if self._consume_tombstone(frame.request_id):
                return
            raise AgentProtocolError("unknown_result_id")
        state.terminal = True
        if frame.ok:
            if state.cancelled:
                self._resolve_drain(state)
                return
            if not state.future.done():
                state.future.set_result(frame.result or {})
            return
        if state.cancelled:
            self._resolve_drain(state)
            return
        if not state.future.done():
            state.future.set_exception(AgentTaskError(frame.error_code or "internal_error", frame.error_message or ""))

    def _spawn_callback(self, frame: CallbackFrame) -> None:
        state = self._requests.get(frame.request_id)
        if state is None or state.cancelled or frame.task_id != state.request.task_id:
            self._send_detached_error(frame.callback_id, "unknown_task")
            return
        handler = state.callbacks.get(frame.name)
        if handler is None:
            self._send_detached_error(frame.callback_id, "callback_not_registered")
            return
        task = asyncio.create_task(
            self._run_callback(state, frame, handler),
            name=f"komari-agent-callback-{frame.name}",
        )
        state.callback_tasks.add(task)
        self._all_callback_tasks.add(task)

    async def _run_callback(
        self,
        state: _RequestState,
        frame: CallbackFrame,
        handler: AgentCallbackHandler,
    ) -> None:
        task = asyncio.current_task()
        try:
            callback = AgentCallbackRequest(name=frame.name, data=frame.data, task=state.request)
            try:
                result = await handler(callback)
            except asyncio.CancelledError:
                self._send_detached_error(frame.callback_id, "cancelled")
                raise
            except Exception:
                await self._send(encode_callback_result_error(frame.callback_id, "callback_failed"))
                return
            try:
                validate_json_value(result)
                encoded = json_payload(result)
            except (TypeError, ValueError):
                await self._send(encode_callback_result_error(frame.callback_id, "callback_failed"))
                return
            if len(encoded) > MAX_CALLBACK_DATA_BYTES:
                await self._send(encode_callback_result_error(frame.callback_id, "callback_failed"))
                return
            await self._send(encode_callback_result_success(frame.callback_id, result))
        finally:
            if task is not None:
                state.callback_tasks.discard(task)
                self._all_callback_tasks.discard(task)

    def _send_detached_error(self, callback_id: str, code: str) -> None:
        async def send() -> None:
            with contextlib.suppress(AgentHostUnavailableError, AgentProtocolError):
                await self._send(encode_callback_result_error(callback_id, code))

        task = asyncio.create_task(send(), name="komari-agent-detached-callback-result")
        self._detached_sends.add(task)
        task.add_done_callback(self._detached_sends.discard)

    async def _cancel_request(self, request_id: str, state: _RequestState) -> None:
        state.cancelled = True
        await self._cancel_callback_tasks(state)
        if state.terminal or not state.request_sent or self._process is None or self._process.returncode is not None:
            return
        drain = asyncio.get_running_loop().create_future()
        state.drain_future = drain
        try:
            await self._send(encode_cancel(request_id))
        except (AgentHostUnavailableError, AgentProtocolError):
            self._resolve_drain(state)
            return
        try:
            await asyncio.wait_for(asyncio.shield(drain), timeout=CANCEL_DRAIN_TIMEOUT)
        except TimeoutError:
            self._tombstone(request_id)
            await self._protocol_failed("cancel_drain_timeout")

    async def _cancel_callback_tasks(self, state: _RequestState) -> None:
        tasks = list(state.callback_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _finish_callback_tasks(self) -> None:
        tasks = list(self._all_callback_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        detached = list(self._detached_sends)
        if detached:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.gather(*detached, return_exceptions=True), timeout=TERMINATE_TIMEOUT)
            for task in detached:
                task.cancel()

    def _resolve_drain(self, state: _RequestState) -> None:
        drain = state.drain_future
        if drain is not None and not drain.done():
            drain.set_result(None)

    async def _send(self, frame: bytes) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise AgentHostUnavailableError("host_closed")
        async with self._write_lock:
            try:
                process.stdin.write(frame)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise AgentHostUnavailableError("process_exited") from exc

    async def _protocol_failed(self, code: str) -> None:
        self._log.warning("[komari_agent] protocol failure: %s", code)
        await self._terminate_process_async()
        await self._on_process_exit("protocol_error")

    async def _on_process_exit(self, code: str) -> None:
        if self._exit_handled:
            return
        self._exit_handled = True
        self._fail_pending(code)
        self._fail_ready(code)
        self._state = HostState.UNAVAILABLE if self._state is not HostState.CLOSED else self._state
        self._readiness = HostReadiness(state=ReadinessState.UNAVAILABLE, bindings=_UNAVAILABLE_BINDINGS)
        await self._finish_callback_tasks()

    def _fail_ready(self, code: str) -> None:
        future = self._ready_future
        if future is not None and not future.done():
            future.set_exception(AgentHostUnavailableError(code))

    def _fail_pending(self, code: str) -> None:
        for state in list(self._requests.values()):
            if not state.future.done():
                state.future.set_exception(AgentHostUnavailableError(code))
            self._resolve_drain(state)

    async def _mark_unavailable(self, code: str) -> None:
        self._fail_ready(code)
        await self._on_process_exit(code)

    async def _abort_process(self) -> None:
        process = self._process
        if process is not None and process.returncode is None:
            await self._terminate_process(process)

    async def _terminate_process_async(self) -> None:
        process = self._process
        if process is not None and process.returncode is None:
            await self._terminate_process(process)

    async def _terminate_process(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=TERMINATE_TIMEOUT)
        except TimeoutError:
            pass
        else:
            return
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=TERMINATE_TIMEOUT)

    async def _stop_reader_and_stderr(self) -> None:
        tasks = [task for task in (self._reader_task, self._stderr_task) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reader_task = None
        self._stderr_task = None

    def _tombstone(self, request_id: str) -> None:
        self._tombstones.append(request_id)
        self._tombstone_set.add(request_id)
        while len(self._tombstones) > _MAX_TOMBSTONES:
            removed = self._tombstones.popleft()
            self._tombstone_set.discard(removed)

    def _consume_tombstone(self, request_id: str) -> bool:
        if request_id not in self._tombstone_set:
            return False
        self._tombstone_set.discard(request_id)
        with contextlib.suppress(ValueError):
            self._tombstones.remove(request_id)
        return True


__all__ = ["AgentHost"]
