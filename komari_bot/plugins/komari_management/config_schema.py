"""Komari Management 配置 Schema。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class DynamicConfigSchema(BaseModel):
    """Komari Management 配置模型。"""

    version: str = Field(default="1.0", description="配置架构版本")
    last_updated: str = Field(
        default_factory=lambda: datetime.now().astimezone().isoformat(),
        description="最后更新时间戳",
    )

    plugin_enable: bool = Field(default=False, description="是否启用统一管理 API 插件")
    api_token: str = Field(default="", description="管理 API Bearer Token")
    api_allowed_origins: list[str] = Field(
        default_factory=list,
        description="允许访问管理 API 的前端 Origin 白名单",
    )

    @field_validator("api_allowed_origins", mode="before")
    @classmethod
    def parse_list_string(cls, value: Any) -> Any:
        """兼容从 .env 读取字符串列表。"""
        if isinstance(value, str):
            import json

            try:
                parsed = json.loads(value)
                return [str(item) for item in parsed]
            except (json.JSONDecodeError, TypeError):
                return [item.strip() for item in value.split(",") if item.strip()]
        return value
