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


class _LoopThread:
    """后台承载事件循环的线程，模拟应用事件循环。"""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)
        self.loop.close()


def test_sync_operations_inside_event_loop_raise(storage_module: Any) -> None:
    storage = storage_module.ConfigStorage()

    async def _caller() -> None:
        with pytest.raises(RuntimeError, match="请改用对应的 _async"):
            storage.fetch("user_data")

    try:
        asyncio.run(_caller())
    finally:
        storage.close()


def test_sync_bridge_timeout_cancels_in_flight_operation(
    storage_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage_module, "_CONFIG_STORAGE_TIMEOUT_SECONDS", 0.01)
    storage = storage_module.ConfigStorage()
    runner = _LoopThread()
    cancelled = threading.Event()

    async def _never_finishes(_session: object) -> str:
        try:
            await asyncio.sleep(60)
            msg = "不应完成"
            raise AssertionError(msg)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def _fake_run_on_app_session(_operation: Any) -> Any:
        return await _never_finishes(None)

    monkeypatch.setattr(
        storage, "_run_on_app_session", _fake_run_on_app_session
    )
    storage.bind_app_loop(runner.loop)

    try:
        with pytest.raises(RuntimeError, match="配置存储操作超时"):
            storage.fetch("user_data")
        assert cancelled.wait(timeout=1.0) is True
    finally:
        asyncio.run_coroutine_threadsafe(
            storage.close_async(), runner.loop
        ).result(timeout=2)
        runner.stop()
        storage.close()


def test_sync_bridge_uses_private_engine_before_app_loop_bind(
    storage_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = storage_module.ConfigStorage()
    calls: list[str] = []

    async def _fake_private_engine(_operation: Any) -> str:
        calls.append("private")
        return "ok"

    async def _fake_app_session(_operation: Any) -> str:
        calls.append("app")
        return "ok"

    monkeypatch.setattr(storage, "_run_on_private_engine", _fake_private_engine)
    monkeypatch.setattr(storage, "_run_on_app_session", _fake_app_session)

    try:
        assert storage.fetch("user_data") == "ok"
        assert calls == ["private"]
    finally:
        storage.close()


def test_sync_bridge_uses_app_session_after_bind(
    storage_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = storage_module.ConfigStorage()
    runner = _LoopThread()
    calls: list[str] = []

    async def _fake_private_engine(_operation: Any) -> str:
        calls.append("private")
        return "ok"

    async def _fake_app_session(_operation: Any) -> str:
        calls.append("app")
        return "ok"

    monkeypatch.setattr(storage, "_run_on_private_engine", _fake_private_engine)
    monkeypatch.setattr(storage, "_run_on_app_session", _fake_app_session)
    storage.bind_app_loop(runner.loop)

    try:
        assert storage.fetch("user_data") == "ok"
        assert calls == ["app"]
    finally:
        asyncio.run_coroutine_threadsafe(
            storage.close_async(), runner.loop
        ).result(timeout=2)
        runner.stop()
        storage.close()


def test_closed_storage_rejects_new_operations(storage_module: Any) -> None:
    storage = storage_module.ConfigStorage()
    storage.close()

    with pytest.raises(RuntimeError, match="配置存储已关闭"):
        storage.fetch("user_data")


@pytest.mark.asyncio
async def test_close_async_cancels_and_awaits_watcher_task(
    storage_module: Any,
) -> None:
    storage = storage_module.ConfigStorage()
    cancelled = threading.Event()

    async def _never_finishes() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    storage._watch_config_changes = _never_finishes  # type: ignore[method-assign]
    storage.bind_app_loop(asyncio.get_running_loop())
    assert storage._watch_task is not None
    await asyncio.sleep(0)

    await storage.close_async()

    assert cancelled.wait(timeout=1.0) is True
    assert storage.closed is True
    with pytest.raises(RuntimeError, match="配置存储已关闭"):
        await storage.fetch_async("user_data")


@pytest.mark.asyncio
async def test_close_config_storage_if_created_does_not_create_storage(
    storage_module: Any,
) -> None:
    storage_module._StorageState.storage = None

    await storage_module.close_config_storage_if_created()

    assert storage_module._StorageState.storage is None


@pytest.mark.asyncio
async def test_close_config_storage_if_created_closes_and_clears_storage(
    storage_module: Any,
) -> None:
    class _FakeStorage:
        closed = False

        async def close_async(self) -> None:
            self.closed = True

    fake = _FakeStorage()
    storage_module._StorageState.storage = fake

    await storage_module.close_config_storage_if_created()

    assert fake.closed is True
    assert storage_module._StorageState.storage is None


def test_private_engine_uses_orm_database_url(
    storage_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_urls: list[str] = []

    class _FakeEngine:
        def __init__(self, url: str) -> None:
            created_urls.append(url)
            self.sync_engine = object()

        async def dispose(self) -> None:
            return None

    async def _simple_op(session: object) -> str:
        del session
        return "ok"

    monkeypatch.setattr(
        storage_module,
        "create_async_engine",
        lambda url: _FakeEngine(url),
    )
    monkeypatch.setattr(
        storage_module,
        "get_orm_database_url",
        lambda: "postgresql+asyncpg://u:p@h:5432/db",
    )

    storage = storage_module.ConfigStorage()
    try:
        result = asyncio.run(storage._run_on_private_engine(_simple_op))
        assert result == "ok"
        assert created_urls == ["postgresql+asyncpg://u:p@h:5432/db"]
    finally:
        storage.close()


def test_config_storage_no_longer_builds_url_from_postgres_config(
    storage_module: Any,
) -> None:
    assert not hasattr(storage_module, "_build_database_url")
    assert not hasattr(storage_module, "get_shared_database_config")
