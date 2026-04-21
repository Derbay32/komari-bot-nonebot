"""Komari Healthcheck 插件配置 Schema。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class HealthCheckConfig(BaseModel):
    """Komari Healthcheck 配置 Schema。"""

    version: str = Field(default="1.0", description="配置架构版本")
    last_updated: str = Field(
        default_factory=lambda: datetime.now().astimezone().isoformat(),
        description="最后更新时间戳",
    )

    plugin_enable: bool = Field(default=True, description="插件启用状态")
    endpoint_path: str = Field(default="/health", description="健康检查端点路径")
    online_message: str = Field(default="OK", description="在线时的响应消息")
    offline_message: str = Field(
        default="Bot is offline",
        description="离线时的响应消息",
    )

    @field_validator("endpoint_path")
    @classmethod
    def validate_endpoint_path(cls, value: str) -> str:
        """规范化健康检查端点路径。"""
        normalized = value.strip()
        if not normalized:
            msg = "endpoint_path 不能为空"
            raise ValueError(msg)
        if not normalized.startswith("/"):
            normalized = f"/{normalized}"
        if len(normalized) > 1:
            normalized = normalized.rstrip("/")
        return normalized
