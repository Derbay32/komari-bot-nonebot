"""Redis 共享配置 schema 与读取辅助。

v2.0.0 起 PostgreSQL 连接配置退役：数据库连接唯一权威是 nonebot-plugin-orm
的 ``SQLALCHEMY_DATABASE_URL``（见 ``komari_bot.db.orm_config``）。
本模块只承载仍无替代品的 Redis 配置（sr / komari_custom 会话、
group_history_summary 群锁、komari_memory Redis 管理器共用的连接参数）。
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import AliasChoices, BaseModel, Field


class RedisConfigSchema(BaseModel):
    """Redis 共享连接配置（由 NoneBot dotenv / 环境变量提供）。"""

    redis_host: str = Field(
        default="localhost",
        validation_alias=AliasChoices("redis_host", "REDIS_HOST"),
        description="Redis 主机地址",
    )
    redis_port: int = Field(
        default=6379,
        validation_alias=AliasChoices("redis_port", "REDIS_PORT"),
        description="Redis 端口",
    )
    redis_password: str = Field(
        default="",
        validation_alias=AliasChoices("redis_password", "REDIS_PASSWORD"),
        description="Redis 密码（空字符串表示无密码）",
    )


def load_redis_config_from_env() -> RedisConfigSchema:
    """从当前进程环境变量读取 Redis 共享配置。"""
    keys = {"REDIS_HOST", "REDIS_PORT", "REDIS_PASSWORD"}
    data: dict[str, object] = {
        key: value for key in keys if (value := os.getenv(key)) is not None
    }
    return RedisConfigSchema.model_validate(data)


@lru_cache(maxsize=1)
def get_shared_redis_config() -> RedisConfigSchema:
    """获取共享 Redis 配置。

    NoneBot 运行时优先使用其已加载的 dotenv 配置；独立脚本或测试环境则
    直接读取进程环境变量。
    """
    try:
        from nonebot import get_driver, get_plugin_config
    except ImportError:
        return load_redis_config_from_env()

    try:
        get_driver()
    except ValueError:
        return load_redis_config_from_env()
    return get_plugin_config(RedisConfigSchema)
