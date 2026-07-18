"""Komari Search 动态配置 Schema。"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class DynamicConfigSchema(BaseModel):
    """Komari Search 插件配置。"""

    version: str = Field(default="1.0", description="配置架构版本")
    last_updated: str = Field(
        default_factory=lambda: datetime.now().astimezone().isoformat(),
        description="最后更新时间戳",
    )

    plugin_enable: bool = Field(default=False, description="插件启用状态")
    user_whitelist: list[str] = Field(default_factory=list, description="用户白名单")
    group_whitelist: list[str] = Field(default_factory=list, description="群聊白名单")

    search_enabled: bool = Field(default=True, description="是否启用 Tavily 联网搜索")
    tavily_api_key: str = Field(default="", description="Tavily API Key")
    search_depth: str = Field(
        default="basic",
        description="搜索深度：basic（快速）/ advanced（深入）",
    )
    max_results: int = Field(default=5, ge=1, le=10, description="最大搜索结果数")
    include_answer: bool = Field(default=True, description="是否包含 Tavily 答案摘要")
    result_content_limit: int = Field(
        default=300,
        ge=80,
        le=1000,
        description="每条搜索结果正文截断长度",
    )
    search_timeout_seconds: float = Field(
        default=12.0,
        ge=1.0,
        le=60.0,
        description="联网搜索排队与请求共享的业务截止时间（秒）",
    )
    circuit_breaker_failure_threshold: int = Field(
        default=3,
        ge=1,
        le=10,
        description="连续失败多少次后暂时熔断搜索服务",
    )
    circuit_breaker_recovery_seconds: float = Field(
        default=30.0,
        ge=1.0,
        le=300.0,
        description="搜索服务熔断后的探测等待时间（秒）",
    )

    @field_validator("user_whitelist", "group_whitelist", mode="before")
    @classmethod
    def parse_list_string(cls, v: Any) -> Any:
        """兼容 JSON 字符串与逗号分隔白名单配置。"""
        if isinstance(v, str):
            import json

            try:
                parsed = json.loads(v)
                return [str(item) for item in parsed]
            except (json.JSONDecodeError, TypeError):
                return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @field_validator("search_depth")
    @classmethod
    def validate_search_depth(cls, v: str) -> str:
        """限制 Tavily search_depth 到官方支持值。"""
        normalized = v.strip().lower()
        if normalized in {"basic", "advanced"}:
            return normalized
        return "basic"
