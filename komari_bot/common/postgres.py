"""进程内共享 PostgreSQL 连接池与连接预算。"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from threading import RLock
from typing import Any, Protocol, cast

import asyncpg
from nonebot import logger


class PostgresConfig(Protocol):
    """共享连接所需的最小 PostgreSQL 配置。"""

    pg_host: str
    pg_port: int
    pg_database: str
    pg_user: str
    pg_password: str


class PostgresPoolBudgetExceededError(RuntimeError):
    """创建新物理池会超过当前进程连接预算。"""


@dataclass(frozen=True, slots=True)
class _PoolKey:
    host: str
    port: int
    database: str
    user: str
    password_fingerprint: bytes
    min_size: int
    max_size: int
    command_timeout: float


@dataclass(slots=True)
class _PoolEntry:
    pool: asyncpg.Pool
    ref_count: int
    max_size: int


_REGISTRY_GUARD = RLock()
_LOOP_LOCKS: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}
_POOL_REGISTRIES: dict[
    asyncio.AbstractEventLoop,
    dict[_PoolKey, _PoolEntry],
] = {}
_reserved_max_connections = 0


def _resolve_pool_size(config: object) -> tuple[int, int]:
    min_size = max(1, int(getattr(config, "pg_pool_min_size", 1)))
    max_size = max(min_size, int(getattr(config, "pg_pool_max_size", 5)))
    return min_size, max_size


def _resolve_process_budget(config: object) -> int:
    return max(1, int(getattr(config, "pg_pool_process_budget", 20)))


def _build_pool_key(
    config: PostgresConfig,
    *,
    min_size: int,
    max_size: int,
    command_timeout: float,
) -> _PoolKey:
    return _PoolKey(
        host=str(config.pg_host),
        port=int(config.pg_port),
        database=str(config.pg_database),
        user=str(config.pg_user),
        password_fingerprint=hashlib.sha256(
            str(config.pg_password).encode("utf-8")
        ).digest(),
        min_size=min_size,
        max_size=max_size,
        command_timeout=command_timeout,
    )


def _get_loop_lock(loop: asyncio.AbstractEventLoop) -> asyncio.Lock:
    with _REGISTRY_GUARD:
        return _LOOP_LOCKS.setdefault(loop, asyncio.Lock())


def _reserve_capacity(*, max_size: int, process_budget: int) -> None:
    global _reserved_max_connections  # noqa: PLW0603
    with _REGISTRY_GUARD:
        projected = _reserved_max_connections + max_size
        if projected > process_budget:
            message = (
                "PostgreSQL 连接池预算不足："
                f"已预留 {_reserved_max_connections}，"
                f"新池需要 {max_size}，进程上限 {process_budget}"
            )
            raise PostgresPoolBudgetExceededError(message)
        _reserved_max_connections = projected


def _release_capacity(max_size: int) -> None:
    global _reserved_max_connections  # noqa: PLW0603
    with _REGISTRY_GUARD:
        _reserved_max_connections = max(0, _reserved_max_connections - max_size)


class _SharedPostgresPoolLease:
    """物理连接池的引用计数租约，对调用方保持 asyncpg Pool 接口。"""

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        key: _PoolKey,
        pool: asyncpg.Pool,
    ) -> None:
        self._loop = loop
        self._key = key
        self._pool = pool
        self._closed = False
        self._close_lock = asyncio.Lock()

    def __getattr__(self, name: str) -> Any:
        if self._closed:
            message = "PostgreSQL 连接池租约已经关闭"
            raise RuntimeError(message)
        return getattr(self._pool, name)

    async def close(self) -> None:
        """仅释放当前租约；最后一个租约负责关闭物理连接池。"""
        async with self._close_lock:
            if self._closed:
                return
            await _release_pool_lease(
                loop=self._loop,
                key=self._key,
                pool=self._pool,
            )
            self._closed = True

    def __repr__(self) -> str:
        return f"<_SharedPostgresPoolLease closed={self._closed}>"


async def _release_pool_lease(
    *,
    loop: asyncio.AbstractEventLoop,
    key: _PoolKey,
    pool: asyncpg.Pool,
) -> None:
    if asyncio.get_running_loop() is not loop:
        message = "PostgreSQL 连接池租约必须在创建它的事件循环中关闭"
        raise RuntimeError(message)

    loop_lock = _get_loop_lock(loop)
    pool_to_close: asyncpg.Pool | None = None
    async with loop_lock:
        registry = _POOL_REGISTRIES.get(loop)
        entry = None if registry is None else registry.get(key)
        if entry is None or entry.pool is not pool:
            return
        entry.ref_count -= 1
        if entry.ref_count > 0:
            logger.debug(
                "[PostgresPool] 已释放共享租约，剩余引用={}",
                entry.ref_count,
            )
            return

        assert registry is not None
        registry.pop(key, None)
        pool_to_close = entry.pool
        _release_capacity(entry.max_size)
        if not registry:
            with _REGISTRY_GUARD:
                _POOL_REGISTRIES.pop(loop, None)
                _LOOP_LOCKS.pop(loop, None)

    if pool_to_close is not None:
        await pool_to_close.close()
        logger.info("[PostgresPool] 最后一个租约已释放，物理连接池已关闭")


async def create_postgres_pool(
    config: PostgresConfig,
    *,
    command_timeout: float = 30,
) -> asyncpg.Pool:
    """按事件循环和连接参数复用物理池，并返回独立关闭租约。"""
    loop = asyncio.get_running_loop()
    min_size, max_size = _resolve_pool_size(config)
    timeout = float(command_timeout)
    key = _build_pool_key(
        config,
        min_size=min_size,
        max_size=max_size,
        command_timeout=timeout,
    )
    loop_lock = _get_loop_lock(loop)

    async with loop_lock:
        registry = _POOL_REGISTRIES.setdefault(loop, {})
        entry = registry.get(key)
        if entry is not None:
            entry.ref_count += 1
            logger.debug(
                "[PostgresPool] 复用物理连接池，当前引用={}",
                entry.ref_count,
            )
            return cast(
                "asyncpg.Pool",
                _SharedPostgresPoolLease(loop=loop, key=key, pool=entry.pool),
            )

        process_budget = _resolve_process_budget(config)
        _reserve_capacity(max_size=max_size, process_budget=process_budget)
        try:
            pool = await asyncpg.create_pool(
                host=config.pg_host,
                port=config.pg_port,
                database=config.pg_database,
                user=config.pg_user,
                password=config.pg_password,
                min_size=min_size,
                max_size=max_size,
                command_timeout=timeout,
            )
        except BaseException:
            _release_capacity(max_size)
            raise

        registry[key] = _PoolEntry(pool=pool, ref_count=1, max_size=max_size)
        stats = get_postgres_pool_stats()
        logger.info(
            "[PostgresPool] 已创建物理连接池：pool_count={} reserved_max={}",
            stats["physical_pool_count"],
            stats["reserved_max_connections"],
        )
        return cast(
            "asyncpg.Pool",
            _SharedPostgresPoolLease(loop=loop, key=key, pool=pool),
        )


def get_postgres_pool_stats() -> dict[str, int]:
    """返回不含连接参数和凭据的进程级池统计。"""
    with _REGISTRY_GUARD:
        entries = [
            entry
            for registry in _POOL_REGISTRIES.values()
            for entry in registry.values()
        ]
        return {
            "event_loop_count": len(_POOL_REGISTRIES),
            "physical_pool_count": len(entries),
            "lease_count": sum(entry.ref_count for entry in entries),
            "reserved_max_connections": _reserved_max_connections,
        }


__all__ = [
    "PostgresPoolBudgetExceededError",
    "create_postgres_pool",
    "get_postgres_pool_stats",
]
