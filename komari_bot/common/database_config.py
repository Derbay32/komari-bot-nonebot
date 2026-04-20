"""共享数据库配置 schema 与读取辅助。"""

from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from pathlib import Path


class DatabaseConfigSchema(BaseModel):
    """共享数据库配置（由 config_manager 管理）。"""

    version: str = Field(default="1.0", description="配置架构版本")
    last_updated: str = Field(
        default_factory=lambda: datetime.now().astimezone().isoformat(),
        description="最后更新时间戳",
    )

    pg_host: str = Field(default="localhost", description="PostgreSQL 主机地址")
    pg_port: int = Field(default=5432, description="PostgreSQL 端口")
    pg_database: str = Field(default="komari_bot", description="数据库名称")
    pg_user: str = Field(default="", description="数据库用户名")
    pg_password: str = Field(default="", description="数据库密码")
    pg_pool_min_size: int = Field(
        default=2, ge=1, le=10, description="PostgreSQL 连接池最小连接数"
    )
    pg_pool_max_size: int = Field(
        default=5, ge=1, le=50, description="PostgreSQL 连接池最大连接数"
    )

    redis_host: str = Field(default="localhost", description="Redis 主机地址")
    redis_port: int = Field(default=6379, description="Redis 端口")
    redis_password: str = Field(
        default="", description="Redis 密码（空字符串表示无密码）"
    )


def load_database_config_from_file(config_path: "Path") -> DatabaseConfigSchema:
    """从 JSON 文件加载共享数据库配置。"""
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")  # noqa: TRY003

    data = json.loads(config_path.read_text(encoding="utf-8"))
    return DatabaseConfigSchema(**data)


@lru_cache(maxsize=1)
def _get_database_config_manager() -> Any:
    from nonebot.plugin import require

    config_manager_plugin = require("config_manager")
    return config_manager_plugin.get_config_manager("database", DatabaseConfigSchema)


def get_shared_database_config() -> DatabaseConfigSchema:
    """获取共享数据库配置。"""
    manager = _get_database_config_manager()
    return manager.get()
