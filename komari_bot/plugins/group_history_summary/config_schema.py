"""群聊历史总结插件配置。"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class DynamicConfigSchema(BaseModel):
    """群聊历史总结插件动态配置。"""

    version: str = Field(default="1.0", description="配置架构版本")
    last_updated: str = Field(
        default_factory=lambda: datetime.now().astimezone().isoformat(),
        description="最后更新时间戳",
    )

    plugin_enable: bool = Field(default=True, description="插件启用状态")

    user_whitelist: list[str] = Field(
        default_factory=list, description="用户白名单，为空则允许所有用户"
    )
    group_whitelist: list[str] = Field(
        default_factory=list, description="群聊白名单，为空则允许所有群聊"
    )

    min_summary_count: int = Field(default=10, ge=1, le=1000, description="最少总结条数")
    max_summary_count: int = Field(
        default=200, ge=1, le=1000, description="最多总结条数"
    )
    fetch_batch_size: int = Field(default=50, ge=1, le=200, description="单次拉取条数")

    summary_model: str = Field(default="deepseek-chat", description="总结模型")
    summary_temperature: float = Field(
        default=0.4, ge=0.0, le=2.0, description="总结温度参数"
    )
    summary_max_tokens: int = Field(
        default=1200, ge=128, le=8192, description="总结最大 tokens"
    )

    card_width: int = Field(default=1080, ge=700, le=2000, description="图片宽度")
    card_font_size: int = Field(default=34, ge=16, le=80, description="图片字体大小")

    @field_validator("user_whitelist", "group_whitelist", mode="before")
    @classmethod
    def parse_list_string(cls, value: Any) -> Any:
        """处理从 .env 格式解析列表。"""
        if isinstance(value, str):
            import json

            try:
                parsed = json.loads(value)
                return [str(item) for item in parsed]
            except (json.JSONDecodeError, TypeError):
                return [item.strip() for item in value.split(",") if item.strip()]
        return value
