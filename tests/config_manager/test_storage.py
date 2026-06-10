"""config_manager 存储层测试。"""

from __future__ import annotations

import asyncio
import threading
from importlib import import_module
from typing import Any

import pytest


@pytest.fixture
def storage_module() -> Any:
    return import_module("komari_bot.plugins.config_manager.storage")


async def _never_finishes(cancelled: threading.Event) -> None:
    try:
        await asyncio.sleep(60)
    except asyncio.CancelledError:
        cancelled.set()
        raise


def test_run_timeout_cancels_future_and_raises_runtime_error(
    storage_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage_module, "_CONFIG_STORAGE_TIMEOUT_SECONDS", 0.01)
    storage = storage_module.ConfigStorage()
    cancelled = threading.Event()

    try:
        with pytest.raises(RuntimeError, match="配置存储操作超时"):
            storage._run(_never_finishes(cancelled))
        assert cancelled.wait(timeout=1.0) is True
    finally:
        storage.close()
