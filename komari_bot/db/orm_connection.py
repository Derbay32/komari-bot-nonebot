"""nonebot-plugin-orm 共享引擎的 asyncpg 兼容连接适配。

背景
----
ticket 10 把 komari_memory / komari_knowledge / komari_help / agent_run_logger
四个插件的数据库连接来源从自研 asyncpg 池切换
到 nonebot-plugin-orm 共享引擎，同时保留这四个插件的 raw SQL（向量检索 /
HNSW / UNLOGGED / advisory lock 的判定依据不动）。

实现形态
--------
选定「共享引擎底层 raw connection」形态：``acquire()`` 每次从 nonebot-plugin-orm
默认引擎借出 ``AsyncConnection``，取其池代理（``get_raw_connection()`` 返回
``_ConnectionFairy``）的 ``driver_connection`` —— 即真正的 asyncpg 连接对象，
对调用方暴露 asyncpg Connection 原生 API。这样：

- 全部现存 SQL 的 ``$n`` 占位符、``fetchrow/fetchval/fetch/execute/executemany``、
  ``transaction()``（含 ``isolation="repeatable_read", readonly=True``）、
  pgvector 向量参数（``$1::vector`` 字符串绑定）、数组参数、advisory xact lock
  语义与旧 asyncpg 池逐字等价，无需改写任何语句；
- 归还语义：``AsyncConnection`` 上下文退出即把连接归还 SQLAlchemy 连接池，
  池回收时对未提交事务执行回滚；调用方保持「事务一定用 ``async with
  conn.transaction()`` 包裹」的既有纪律即可；
- 配置统一从 nonebot-plugin-orm 读取（``SQLALCHEMY_DATABASE_URL``），本模块
  不再消费任何自研 PG 连接配置（v2.0.0 已删除）；
- 引擎生命周期由 nonebot-plugin-orm 托管：``close()`` 是无操作，插件关闭时
  不得 dispose 共享引擎（会破坏其他插件）。

类型约定：生产代码类型注解统一使用本模块的 ``SharedEngineConnectionPool``；
由于它按 asyncpg Pool 接口子集实现，``TYPE_CHECKING`` 块中的 asyncpg 类型
注解保持不变。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from threading import Lock
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import asyncpg
    from sqlalchemy.ext.asyncio import AsyncEngine


class SharedEngineConnectionPool:
    """nonebot-plugin-orm 共享引擎的 asyncpg 兼容连接租约。

    对调用方保持 ``asyncpg.Pool`` 的 ``acquire()`` 接口子集；每次 acquire
    都从共享引擎借出底层 asyncpg 连接，用完即归还，不持有常驻连接。
    """

    def __init__(self) -> None:
        self._closed = False

    @staticmethod
    def _shared_engine() -> "AsyncEngine":
        """惰性获取 nonebot-plugin-orm 默认引擎（触发 ORM 初始化）。"""
        from nonebot_plugin_orm import get_session

        session = get_session()
        # 会话仅用于读取绑定的引擎，未借出任何连接，直接随 GC 释放即可
        return cast("AsyncEngine", session.bind)

    @asynccontextmanager
    async def acquire(self) -> "AsyncIterator[asyncpg.Connection]":
        """借出共享引擎底层 asyncpg 连接。

        ``AsyncConnection`` 上下文退出时归还连接池；若调用方遗留未提交
        事务，SQLAlchemy 池回收逻辑会回滚，不会污染下一个借用者。
        """
        engine = self._shared_engine()
        async with engine.connect() as connection:
            raw = await connection.get_raw_connection()
            driver = cast("asyncpg.Connection", raw.driver_connection)
            yield driver

    async def probe(self) -> bool:
        """探测数据库可达性（初始化阶段用于保持降级语义）。"""
        try:
            async with self.acquire() as conn:
                value = await conn.fetchval("SELECT 1")
                return value == 1
        except Exception:
            return False

    async def close(self) -> None:
        """无操作：共享引擎由 nonebot-plugin-orm 托管，插件关闭不 dispose。"""
        self._closed = True


_shared_pool: SharedEngineConnectionPool | None = None
_pool_creation_lock = Lock()


def get_shared_orm_connection_pool() -> SharedEngineConnectionPool:
    """返回进程级共享的引擎连接租约单例。"""
    global _shared_pool  # noqa: PLW0603
    if _shared_pool is None:
        with _pool_creation_lock:
            if _shared_pool is None:
                _shared_pool = SharedEngineConnectionPool()
    return _shared_pool


__all__ = [
    "SharedEngineConnectionPool",
    "get_shared_orm_connection_pool",
]
