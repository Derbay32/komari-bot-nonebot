"""基于 Redis 的群总结分布式租约。"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import redis.asyncio as aioredis
from nonebot import logger

from komari_bot.common.database_config import get_shared_database_config

if TYPE_CHECKING:
    from collections.abc import Coroutine


_LOCK_PREFIX = "komari:group_history_summary:lock"
_RENEW_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""
_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


class GroupSummaryLockLostError(RuntimeError):
    """群总结任务执行期间失去租约。"""


@dataclass(slots=True)
class GroupSummaryLease:
    """一条可自动续租、可校验 owner 释放的群总结租约。"""

    manager: GroupSummaryLockManager
    key: str
    owner_token: str
    ttl_seconds: int
    _lost_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _renew_task: asyncio.Task[None] | None = field(default=None, init=False)
    _closed: bool = field(default=False, init=False)

    def start_renewal(self) -> None:
        """启动后台续租。"""
        self._renew_task = asyncio.create_task(
            self._renew_loop(),
            name=f"group-summary-lock-renew:{self.key}",
        )

    async def _renew_loop(self) -> None:
        interval_seconds = max(1.0, self.ttl_seconds / 3)
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                renewed = await self.manager.renew(
                    key=self.key,
                    owner_token=self.owner_token,
                    ttl_seconds=self.ttl_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "[GroupHistorySummary] 群总结租约续期失败: key={} error_type={}",
                    self.key,
                    type(exc).__name__,
                )
                self._lost_event.set()
                return

            if not renewed:
                logger.warning(
                    "[GroupHistorySummary] 群总结租约已被接管: key={}",
                    self.key,
                )
                self._lost_event.set()
                return

    async def run[T](self, operation: Coroutine[Any, Any, T]) -> T:
        """执行任务；若租约中途丢失则取消任务并报错。"""
        operation_task = asyncio.create_task(operation)
        lost_task = asyncio.create_task(self._lost_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {operation_task, lost_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if lost_task in done and self._lost_event.is_set():
                operation_task.cancel()
                with suppress(asyncio.CancelledError):
                    await operation_task
                raise GroupSummaryLockLostError("群总结分布式租约已失效")
            return await operation_task
        finally:
            if not operation_task.done():
                operation_task.cancel()
                with suppress(asyncio.CancelledError):
                    await operation_task
            lost_task.cancel()
            with suppress(asyncio.CancelledError):
                await lost_task

    async def close(self) -> None:
        """停止续租，并仅在 owner 匹配时释放租约。"""
        if self._closed:
            return
        self._closed = True

        if self._renew_task is not None:
            self._renew_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._renew_task
            self._renew_task = None

        try:
            released = await self.manager.release(
                key=self.key,
                owner_token=self.owner_token,
            )
        except Exception as exc:
            logger.error(
                "[GroupHistorySummary] 群总结租约释放失败: key={} error_type={}",
                self.key,
                type(exc).__name__,
            )
            return

        if not released and not self._lost_event.is_set():
            logger.warning(
                "[GroupHistorySummary] 群总结租约释放被拒绝: key={}",
                self.key,
            )


class GroupSummaryLockManager:
    """管理群总结 Redis 客户端和分布式租约。"""

    def __init__(self) -> None:
        self._redis: aioredis.Redis | None = None
        self._client_lock: asyncio.Lock | None = None
        self._client_lock_loop: asyncio.AbstractEventLoop | None = None

    def _get_client_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._client_lock is None or self._client_lock_loop is not loop:
            self._client_lock = asyncio.Lock()
            self._client_lock_loop = loop
        return self._client_lock

    async def _get_client(self, redis_db: int) -> aioredis.Redis:
        if self._redis is not None:
            return self._redis

        async with self._get_client_lock():
            if self._redis is None:
                database_config = get_shared_database_config()
                client = aioredis.Redis(
                    host=database_config.redis_host,
                    port=database_config.redis_port,
                    db=redis_db,
                    password=database_config.redis_password or None,
                    decode_responses=True,
                    encoding="utf-8",
                    socket_connect_timeout=5.0,
                    socket_timeout=5.0,
                    health_check_interval=30,
                )
                try:
                    await cast("Any", client.ping())
                except Exception:
                    await client.aclose()
                    raise
                self._redis = client
        return self._redis

    @staticmethod
    def _key(group_id: str) -> str:
        return f"{_LOCK_PREFIX}:{group_id}"

    async def try_acquire(
        self,
        *,
        group_id: str,
        redis_db: int,
        ttl_seconds: int,
    ) -> GroupSummaryLease | None:
        """尝试获取群租约，已被占用时返回 ``None``。"""
        client = await self._get_client(redis_db)
        owner_token = uuid4().hex
        key = self._key(group_id)
        acquired = await client.set(
            key,
            owner_token,
            nx=True,
            px=max(1, ttl_seconds) * 1000,
        )
        if not acquired:
            return None

        lease = GroupSummaryLease(
            manager=self,
            key=key,
            owner_token=owner_token,
            ttl_seconds=max(1, ttl_seconds),
        )
        lease.start_renewal()
        return lease

    async def renew(
        self,
        *,
        key: str,
        owner_token: str,
        ttl_seconds: int,
    ) -> bool:
        """仅由当前 owner 延长租约。"""
        if self._redis is None:
            return False
        result = await self._redis.execute_command(
            "EVAL",
            _RENEW_SCRIPT,
            1,
            key,
            owner_token,
            max(1, ttl_seconds) * 1000,
        )
        return int(cast("int | str | bytes", result)) == 1

    async def release(self, *, key: str, owner_token: str) -> bool:
        """仅由当前 owner 删除租约。"""
        if self._redis is None:
            return False
        result = await self._redis.execute_command(
            "EVAL",
            _RELEASE_SCRIPT,
            1,
            key,
            owner_token,
        )
        return int(cast("int | str | bytes", result)) == 1

    async def close(self) -> None:
        """关闭共享 Redis 连接。"""
        async with self._get_client_lock():
            client = self._redis
            self._redis = None
            if client is not None:
                await client.aclose()


group_summary_lock_manager = GroupSummaryLockManager()


async def close_group_summary_lock_manager() -> None:
    """关闭默认群总结锁管理器。"""
    await group_summary_lock_manager.close()


__all__ = [
    "GroupSummaryLease",
    "GroupSummaryLockLostError",
    "GroupSummaryLockManager",
    "close_group_summary_lock_manager",
    "group_summary_lock_manager",
]
