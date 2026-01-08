"""Komari Memory 数据库连接管理（用于向量检索）。"""

import asyncpg

from ..config_schema import KomariMemoryConfigSchema


async def create_pool(config: KomariMemoryConfigSchema) -> asyncpg.Pool:
    """创建 PostgreSQL 连接池（用于向量检索）。

    Args:
        config: 插件配置

    Returns:
        asyncpg 连接池
    """
    return await asyncpg.create_pool(
        host=config.pg_host,
        port=config.pg_port,
        database=config.pg_database,
        user=config.pg_user,
        password=config.pg_password,
        min_size=config.pg_pool_min_size,
        max_size=config.pg_pool_max_size,
    )
