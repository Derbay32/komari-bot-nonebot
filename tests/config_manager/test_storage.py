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


@pytest.mark.asyncio
async def test_run_async_does_not_block_caller_event_loop(
    storage_module: Any,
) -> None:
    storage = storage_module.ConfigStorage()

    async def delayed_result() -> str:
        await asyncio.sleep(0.1)
        return "完成"

    try:
        operation = asyncio.create_task(storage._run_async(delayed_result()))
        await asyncio.sleep(0)

        assert operation.done() is False
        assert await operation == "完成"
    finally:
        storage.close()


class _FakePool:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def test_close_reclaims_pool_thread_and_event_loop(
    storage_module: Any,
) -> None:
    storage = storage_module.ConfigStorage()
    pool = _FakePool()
    storage._pool = pool
    thread = storage._thread
    loop = storage._loop

    storage.close()
    storage.close()

    assert pool.closed is True
    assert thread.is_alive() is False
    assert loop.is_closed() is True


def test_closed_storage_rejects_new_operations_and_closes_coroutine(
    storage_module: Any,
) -> None:
    storage = storage_module.ConfigStorage()
    storage.close()

    async def _completed() -> str:
        return "不应执行"

    coro = _completed()
    with pytest.raises(RuntimeError, match="配置存储已关闭"):
        storage._run(coro)

    assert coro.cr_frame is None


def test_get_pool_closes_temporary_pool_when_schema_initialization_fails(
    storage_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = storage_module.ConfigStorage()
    pool = _FakePool()

    async def fake_create_postgres_pool(_config: object) -> _FakePool:
        return pool

    async def fail_ensure_schema(_pool: _FakePool) -> None:
        msg = "建表失败"
        raise RuntimeError(msg)

    monkeypatch.setattr(storage_module, "create_postgres_pool", fake_create_postgres_pool)
    monkeypatch.setattr(storage, "_ensure_schema", fail_ensure_schema)

    try:
        with pytest.raises(RuntimeError, match="建表失败"):
            storage._run(storage._get_pool())

        assert pool.closed is True
        assert storage._pool is None
    finally:
        storage.close()


class _FakeStorage:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_close_config_storage_if_created_does_not_create_storage(
    storage_module: Any,
) -> None:
    storage_module._StorageState.storage = None

    storage_module.close_config_storage_if_created()

    assert storage_module._StorageState.storage is None


def test_close_config_storage_if_created_closes_and_clears_storage(
    storage_module: Any,
) -> None:
    storage = _FakeStorage()
    storage_module._StorageState.storage = storage

    storage_module.close_config_storage_if_created()

    assert storage.closed is True
    assert storage_module._StorageState.storage is None
