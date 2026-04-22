"""Komari Status 插件配置 Schema。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class StatusConfig(BaseModel):
    """Komari Status 配置 Schema。"""

    version: str = Field(default="1.0", description="配置架构版本")
    last_updated: str = Field(
        default_factory=lambda: datetime.now().astimezone().isoformat(),
        description="最后更新时间戳",
    )

    plugin_enable: bool = Field(default=True, description="插件启用状态")

    uptime_kuma_url: str = Field(
        default="https://uptime.derbaynas.top:2096",
        description="Uptime Kuma 地址",
    )
    uptime_kuma_username: str = Field(default="", description="Uptime Kuma 用户名")
    uptime_kuma_password: str = Field(default="", description="Uptime Kuma 密码")

    status_page_slug: str = Field(default="komari-bot", description="状态页 slug")
    status_page_url: str = Field(
        default="https://uptime.derbaynas.top:2096/status/komari-bot/",
        description="状态页链接",
    )

    request_timeout: int = Field(default=15, description="请求超时时间（秒）")
    cache_ttl: int = Field(default=60, description="缓存有效期（秒）")
    ssl_verify: bool = Field(default=False, description="是否验证 SSL 证书")

    @field_validator("uptime_kuma_url", "status_page_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        """校验并规范化 URL。"""
        normalized = value.strip()
        if not normalized:
            msg = "URL 不能为空"
            raise ValueError(msg)
        if not normalized.startswith(("http://", "https://")):
            msg = "URL 必须以 http:// 或 https:// 开头"
            raise ValueError(msg)
        return (
            normalized.rstrip("/") + "/"
            if "status/" in normalized
            else normalized.rstrip("/")
        )

    @field_validator("status_page_slug")
    @classmethod
    def validate_status_page_slug(cls, value: str) -> str:
        """校验状态页 slug。"""
        normalized = value.strip().strip("/")
        if not normalized:
            msg = "status_page_slug 不能为空"
            raise ValueError(msg)
        return normalized

    @field_validator("request_timeout")
    @classmethod
    def validate_request_timeout(cls, value: int) -> int:
        """校验请求超时时间。"""
        if value <= 0:
            msg = "request_timeout 必须大于 0"
            raise ValueError(msg)
        return value

    @field_validator("cache_ttl")
    @classmethod
    def validate_cache_ttl(cls, value: int) -> int:
        """校验缓存时间。"""
        if value < 0:
            msg = "cache_ttl 不能小于 0"
            raise ValueError(msg)
        return value
