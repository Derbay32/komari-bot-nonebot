"""WebUI runtime shutdown helper tests."""

from __future__ import annotations

from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import TYPE_CHECKING, Any

from komari_bot.plugins.komari_knowledge.webui_runtime import (
    shutdown_background_context,
)

if TYPE_CHECKING:
    from pytest import LogCaptureFixture


class _FakeLoop:
    def __init__(self, *, running: bool = True, closed: bool = False) -> None:
        self._running = running
        self._closed = closed
        self.stop_calls = 0
        self.close_calls = 0

    def is_running(self) -> bool:
        return self._running

    def is_closed(self) -> bool:
        return self._closed

    def call_soon_threadsafe(self, callback: Any) -> None:
        self.stop_calls += 1
        callback()
        self._running = False

    def stop(self) -> None:
        return None

    def close(self) -> None:
        self.close_calls += 1
        self._closed = True


class _FakeThread:
    def __init__(self, *, alive_after_join: bool = False) -> None:
        self.alive_after_join = alive_after_join
        self.join_calls = 0

    def join(self, timeout: float | None = None) -> None:
        del timeout
        self.join_calls += 1

    def is_alive(self) -> bool:
        return self.alive_after_join


class _TimeoutFuture:
    def __init__(self) -> None:
        self.cancel_called = False

    def result(self, timeout: float | None = None) -> object:
        del timeout
        raise FutureTimeoutError

    def cancel(self) -> bool:
        self.cancel_called = True
        return True


def test_shutdown_background_context_logs_timeout_and_cancels_future(
    caplog: LogCaptureFixture,
) -> None:
    future = _TimeoutFuture()
    loop = _FakeLoop()
    thread = _FakeThread()

    with caplog.at_level("WARNING"):
        shutdown_background_context(
            close_future_factory=lambda: future,
            loop=loop,  # type: ignore[arg-type]
            thread=thread,  # type: ignore[arg-type]
        )

    assert future.cancel_called is True
    assert loop.stop_calls == 1
    assert thread.join_calls == 1
    assert loop.close_calls == 1
    assert "关闭超时" in caplog.text


def test_shutdown_background_context_logs_close_failure_and_continues(
    caplog: LogCaptureFixture,
) -> None:
    loop = _FakeLoop()
    thread = _FakeThread()

    def _raise_error() -> Future[object]:
        future: Future[object] = Future()
        future.set_exception(RuntimeError("boom"))
        return future

    with caplog.at_level("ERROR"):
        shutdown_background_context(
            close_future_factory=_raise_error,
            loop=loop,  # type: ignore[arg-type]
            thread=thread,  # type: ignore[arg-type]
        )

    assert loop.stop_calls == 1
    assert thread.join_calls == 1
    assert loop.close_calls == 1
    assert "关闭失败" in caplog.text


def test_shutdown_background_context_warns_when_thread_stays_alive(
    caplog: LogCaptureFixture,
) -> None:
    future: Future[object] = Future()
    future.set_result(None)
    loop = _FakeLoop()
    thread = _FakeThread(alive_after_join=True)

    with caplog.at_level("WARNING"):
        shutdown_background_context(
            close_future_factory=lambda: future,
            loop=loop,  # type: ignore[arg-type]
            thread=thread,  # type: ignore[arg-type]
        )

    assert loop.stop_calls == 1
    assert thread.join_calls == 1
    assert loop.close_calls == 0
    assert "后台线程未在超时内退出" in caplog.text
