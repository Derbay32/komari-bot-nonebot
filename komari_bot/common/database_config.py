"""共享数据库配置 schema 与读取辅助。"""

from __future__ import annotations

import json
import os
from datetime import datetime
from functools import lru_cache
from typing import TYPE_CHECKING

from pydantic import AliasChoices, BaseModel, Field

if TYPE_CHECKING:
    from pathlib import Path


class DatabaseConfigSchema(BaseModel):
    """共享数据库配置（由 NoneBot dotenv / 环境变量提供）。"""

    version: str = Field(default="1.0", description="配置架构版本")
    last_updated: str = Field(
        default_factory=lambda: datetime.now().astimezone().isoformat(),
        description="最后更新时间戳",
    )

    pg_host: str = Field(
        default="localhost",
        validation_alias=AliasChoices("pg_host", "PG_HOST"),
        description="PostgreSQL 主机地址",
    )
    pg_port: int = Field(
        default=5432,
        validation_alias=AliasChoices("pg_port", "PG_PORT"),
        description="PostgreSQL 端口",
    )
    pg_database: str = Field(
        default="komari_bot",
        validation_alias=AliasChoices("pg_database", "PG_DATABASE"),
        description="数据库名称",
    )
    pg_user: str = Field(
        default="",
        validation_alias=AliasChoices("pg_user", "PG_USER"),
        description="数据库用户名",
    )
    pg_password: str = Field(
        default="",
        validation_alias=AliasChoices("pg_password", "PG_PASSWORD"),
        description="数据库密码",
    )
    pg_pool_min_size: int = Field(
        default=2,
        ge=1,
        le=10,
        validation_alias=AliasChoices("pg_pool_min_size", "PG_POOL_MIN_SIZE"),
        description="PostgreSQL 连接池最小连接数",
    )
    pg_pool_max_size: int = Field(
        default=5,
        ge=1,
        le=50,
        validation_alias=AliasChoices("pg_pool_max_size", "PG_POOL_MAX_SIZE"),
        description="PostgreSQL 连接池最大连接数",
    )

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


def load_database_config_from_file(config_path: "Path") -> DatabaseConfigSchema:
    """从显式指定的旧 JSON 文件加载共享数据库配置。

    该函数仅用于迁移脚本的显式兼容路径，运行时默认配置不再读取
    ``database_config.json``。
    """
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")  # noqa: TRY003

    data = json.loads(config_path.read_text(encoding="utf-8"))
    return DatabaseConfigSchema(**data)


def load_database_config_from_env() -> DatabaseConfigSchema:
    """从当前进程环境变量读取共享数据库配置。"""
    keys = {
        "PG_HOST",
        "PG_PORT",
        "PG_DATABASE",
        "PG_USER",
        "PG_PASSWORD",
        "PG_POOL_MIN_SIZE",
        "PG_POOL_MAX_SIZE",
        "REDIS_HOST",
        "REDIS_PORT",
        "REDIS_PASSWORD",
    }
    data: dict[str, object] = {
        key: value for key in keys if (value := os.getenv(key)) is not None
    }
    return DatabaseConfigSchema.model_validate(data)


@lru_cache(maxsize=1)
def get_shared_database_config() -> DatabaseConfigSchema:
    """获取共享数据库配置。

    NoneBot 运行时优先使用其已加载的 dotenv 配置；独立脚本或测试环境则
    直接读取进程环境变量。两种路径都不会访问 ``database_config.json``。
    """
    try:
        from nonebot import get_plugin_config

        return get_plugin_config(DatabaseConfigSchema)
    except Exception:
        return load_database_config_from_env()
