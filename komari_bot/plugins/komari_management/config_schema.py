"""Komari Management 配置 Schema。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

DEFAULT_ANNOUNCE_STATUS_PAGE_URL = "https://your.status.page/url/here"


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
    announce_status_page_url: str = Field(
        default=DEFAULT_ANNOUNCE_STATUS_PAGE_URL,
        description="维护通知中使用的状态页面链接",
    )
    announce_max_group_count: int = Field(
        default=20,
        ge=1,
        le=100,
        description="单次维护通知最多发送的群数量",
    )
    announce_send_interval_seconds: float = Field(
        default=1.0,
        ge=0.0,
        le=60.0,
        description="维护通知逐群发送间隔秒数",
    )
    announce_request_cooldown_seconds: float = Field(
        default=30.0,
        ge=0.0,
        le=3600.0,
        description="维护通知请求级冷却秒数",
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

    @field_validator("announce_status_page_url")
    @classmethod
    def validate_announce_status_page_url(cls, value: str) -> str:
        """校验维护通知状态页链接。"""
        normalized = value.strip()
        if not normalized:
            msg = "announce_status_page_url 不能为空"
            raise ValueError(msg)
        return normalized
