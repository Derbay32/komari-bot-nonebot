"""群总结 Redis 分布式租约测试。"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from komari_bot.plugins.group_history_summary.group_lock import (
    GroupSummaryLockLostError,
    GroupSummaryLockManager,
)


class _FakeRedisBackend:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}


class _FakeRedis:
    def __init__(self, backend: _FakeRedisBackend) -> None:
        self.backend = backend
        self.closed = False

    async def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool,
        px: int,
    ) -> bool:
        assert nx is True
        assert px > 0
        if key in self.backend.values:
            return False
        self.backend.values[key] = value
        return True

    async def execute_command(self, *args: object) -> int:
        command, script, key_count, key, owner_token, *_rest = args
        assert command == "EVAL"
        assert key_count == 1
        key_text = str(key)
        owner_text = str(owner_token)
        if self.backend.values.get(key_text) != owner_text:
            return 0
        if "PEXPIRE" in str(script):
            return 1
        if "DEL" in str(script):
            del self.backend.values[key_text]
            return 1
        raise AssertionError

    async def aclose(self) -> None:
        self.closed = True


def _manager(backend: _FakeRedisBackend) -> tuple[GroupSummaryLockManager, _FakeRedis]:
    manager = GroupSummaryLockManager()
    client = _FakeRedis(backend)
    manager._redis = cast("Any", client)
    return manager, client


@pytest.mark.asyncio
async def test_two_managers_cannot_acquire_the_same_group() -> None:
    backend = _FakeRedisBackend()
    first_manager, _first_client = _manager(backend)
    second_manager, _second_client = _manager(backend)

    first_lease = await first_manager.try_acquire(
        group_id="10000",
        redis_db=0,
        ttl_seconds=60,
    )
    second_lease = await second_manager.try_acquire(
        group_id="10000",
        redis_db=0,
        ttl_seconds=60,
    )

    assert first_lease is not None
    assert second_lease is None
    await first_lease.close()


@pytest.mark.asyncio
async def test_old_owner_cannot_release_replacement_lease() -> None:
    backend = _FakeRedisBackend()
    manager, _client = _manager(backend)
    lease = await manager.try_acquire(
        group_id="10000",
        redis_db=0,
        ttl_seconds=60,
    )
    assert lease is not None

    backend.values[lease.key] = "replacement-owner"
    await lease.close()

    assert backend.values[lease.key] == "replacement-owner"


@pytest.mark.asyncio
async def test_owner_checked_renewal_and_client_close() -> None:
    backend = _FakeRedisBackend()
    manager, client = _manager(backend)
    lease = await manager.try_acquire(
        group_id="10000",
        redis_db=0,
        ttl_seconds=60,
    )
    assert lease is not None

    assert await manager.renew(
        key=lease.key,
        owner_token=lease.owner_token,
        ttl_seconds=60,
    )
    assert not await manager.renew(
        key=lease.key,
        owner_token="old-owner",
        ttl_seconds=60,
    )

    await lease.close()
    await manager.close()
    assert client.closed is True


@pytest.mark.asyncio
async def test_lost_lease_cancels_inflight_summary() -> None:
    backend = _FakeRedisBackend()
    manager, _client = _manager(backend)
    lease = await manager.try_acquire(
        group_id="10000",
        redis_db=0,
        ttl_seconds=60,
    )
    assert lease is not None

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _operation() -> None:
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    task = asyncio.create_task(lease.run(_operation()))
    await started.wait()
    lease._lost_event.set()

    with pytest.raises(GroupSummaryLockLostError):
        await task
    assert cancelled.is_set()
    await lease.close()


@pytest.mark.asyncio
async def test_caller_cancellation_does_not_leave_detached_summary() -> None:
    backend = _FakeRedisBackend()
    manager, _client = _manager(backend)
    lease = await manager.try_acquire(
        group_id="10000",
        redis_db=0,
        ttl_seconds=60,
    )
    assert lease is not None

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _operation() -> None:
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    task = asyncio.create_task(lease.run(_operation()))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    await lease.close()
