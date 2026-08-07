"""Komari Memory 数据库连接管理（用于向量检索）。

连接来源为 nonebot-plugin-orm 共享引擎（配置统一从 ``SQLALCHEMY_DATABASE_URL``
读取），本模块不再创建自研 asyncpg 池；``create_pool()`` 返回的租约对调用方
保持 asyncpg Pool 的 ``acquire()`` 接口子集，四层记忆系统的全部 raw SQL
（向量检索 / 数组绑定 / 事务边界）逐字不变。
"""

from komari_bot.db.orm_connection import (
    SharedEngineConnectionPool,
    get_shared_orm_connection_pool,
)

__all__ = [
    "SharedEngineConnectionPool",
    "create_pool",
]


async def create_pool() -> SharedEngineConnectionPool:
    """返回 nonebot-plugin-orm 共享引擎的 asyncpg 兼容连接租约。"""
    return get_shared_orm_connection_pool()
