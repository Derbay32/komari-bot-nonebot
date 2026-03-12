"""Shared shutdown helpers for the Streamlit knowledge WebUI."""

from __future__ import annotations

import logging
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import asyncio
    import threading


class CloseFutureProtocol(Protocol):
    """Minimal future contract used by WebUI shutdown."""

    def result(self, timeout: float | None = None) -> object: ...

    def cancel(self) -> bool: ...

logger = logging.getLogger("komari_knowledge.webui")


def shutdown_background_context(
    *,
    close_future_factory: CloseFutureProtocolFactory,
    loop: asyncio.AbstractEventLoop,
    thread: threading.Thread,
    timeout: float = 5.0,
) -> None:
    """Close the background engine, then stop and tear down the loop thread."""
    close_future = close_future_factory()

    try:
        close_future.result(timeout=timeout)
    except FutureTimeoutError:
        close_future.cancel()
        logger.warning("[Komari Knowledge] WebUI 后台引擎关闭超时，准备停止事件循环")
    except Exception:
        logger.exception("[Komari Knowledge] WebUI 后台引擎关闭失败，继续停止事件循环")

    if loop.is_running():
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=timeout)
        if thread.is_alive():
            logger.warning("[Komari Knowledge] WebUI 后台线程未在超时内退出")

    if not thread.is_alive() and not loop.is_closed():
        try:
            loop.close()
        except Exception:
            logger.exception("[Komari Knowledge] WebUI 事件循环关闭失败")


class CloseFutureProtocolFactory(Protocol):
    """Factory that returns a future-like object for closing WebUI state."""

    def __call__(self) -> CloseFutureProtocol: ...
