"""Komari Search 动态配置 Schema（v2.0：双提供者 + 网页抓取）。"""

from typing import Any, ClassVar, Literal

from pydantic import field_validator
from sqlalchemy import Column, String
from sqlalchemy.dialects.postgresql import JSONB

from komari_bot.config.typed_config import Field, TypedConfigModel, typed_model_config


class DynamicConfigSchema(TypedConfigModel, table=True):
    """Komari Search 插件配置。"""

    plugin_name: ClassVar[str] = "komari_search"
    __tablename__ = "komari_search_config"

    model_config = typed_model_config(json_schema_extra={"default_apply_mode": "immediate"})

    plugin_enable: bool = Field(default=False, description="插件启用状态")
    user_whitelist: list[str] = Field(
        default_factory=list, description="用户白名单", sa_type=JSONB
    )
    group_whitelist: list[str] = Field(
        default_factory=list, description="群聊白名单", sa_type=JSONB
    )

    search_provider: Literal["tavily", "exa"] = Field(
        default="tavily",
        sa_column=Column(String(16), nullable=False, default="tavily"),
        description="联网搜索提供者：tavily / exa",
    )
    search_api_key: str = Field(
        default="",
        description="联网搜索 API Key（对应 search_provider 填写）",
        json_schema_extra={"secret": True},
    )

    # --- 搜索 ---
    search_enabled: bool = Field(default=True, description="是否启用联网搜索")
    max_results: int = Field(default=5, ge=1, le=10, description="最大搜索结果数")
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

    # --- 抓取 ---
    fetch_enabled: bool = Field(default=True, description="是否启用网页抓取")
    fetch_max_urls: int = Field(
        default=3,
        ge=1,
        le=5,
        description="单次抓取允许的最大 URL 数量",
    )
    fetch_content_limit: int = Field(
        default=3000,
        ge=500,
        le=10000,
        description="每个抓取页面正文截断长度",
    )
    fetch_timeout_seconds: float = Field(
        default=15.0,
        ge=1.0,
        le=60.0,
        description="网页抓取排队与请求共享的业务截止时间（秒）",
    )

    # --- Tavily 专用 ---
    tavily_search_depth: Literal["basic", "advanced"] = Field(
        default="basic",
        sa_column=Column(String(16), nullable=False, default="basic"),
        description="Tavily 搜索深度：basic（快速）/ advanced（深入）",
    )
    tavily_include_answer: bool = Field(
        default=True,
        description="是否包含 Tavily 答案摘要",
    )

    # --- EXA 专用 ---
    exa_search_type: Literal["neural", "keyword", "auto"] = Field(
        default="auto",
        sa_column=Column(String(16), nullable=False, default="auto"),
        description="EXA 搜索类型：neural / keyword / auto",
    )
    exa_fetch_format: Literal["text", "highlights", "summary"] = Field(
        default="text",
        sa_column=Column(String(16), nullable=False, default="text"),
        description="EXA 抓取正文格式：text / highlights / summary",
    )

    # --- 熔断（search/fetch 共用阈值，运行时状态独立） ---
    circuit_breaker_failure_threshold: int = Field(
        default=3,
        ge=1,
        le=10,
        description="连续失败多少次后暂时熔断服务",
    )
    circuit_breaker_recovery_seconds: float = Field(
        default=30.0,
        ge=1.0,
        le=300.0,
        description="服务熔断后的探测等待时间（秒）",
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
