"""Komari Memory 数据库连接管理（用于向量检索）。"""

import asyncpg

from komari_bot.common.database_config import get_shared_database_config
from komari_bot.common.postgres import create_postgres_pool


async def create_pool() -> asyncpg.Pool:
    """根据共享数据库配置创建 PostgreSQL 连接池。"""
    db_config = get_shared_database_config()
    return await create_postgres_pool(db_config)
