"""Komari Memory 数据库连接管理（用于向量检索）。"""

import asyncpg

from komari_bot.common.database_config import get_shared_database_config
from komari_bot.common.postgres import create_postgres_pool

from ..config_schema import KomariMemoryConfigSchema


async def create_pool(config: KomariMemoryConfigSchema) -> asyncpg.Pool:
    """创建 PostgreSQL 连接池（用于向量检索）。

    Args:
        config: 插件配置

    Returns:
        asyncpg 连接池
    """
    del config
    db_config = get_shared_database_config()
    return await create_postgres_pool(db_config)
