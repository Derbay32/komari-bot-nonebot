"""群聊历史总结插件配置。"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class LayoutParamsSchema(BaseModel):
    """图片布局参数。"""

    canvas_width: int = Field(default=1365, ge=600, le=3000)
    canvas_height: int = Field(default=645, ge=300, le=2000)
    bg_color: str = Field(default="#444444")

    title_x: int = Field(default=110, ge=0, le=5000)
    title_y: int = Field(default=80, ge=0, le=5000)
    title_size: int = Field(default=64, ge=10, le=300)
    title_color: str = Field(default="#FFFFFF")

    body_x: int = Field(default=112, ge=0, le=5000)
    body_y: int = Field(default=185, ge=0, le=5000)
    body_size: int = Field(default=30, ge=10, le=300)
    body_color: str = Field(default="#F3F3F3")
    body_line_gap: int = Field(default=10, ge=0, le=100)
    body_max_width: int = Field(default=750, ge=100, le=5000)

    char_enabled: bool = Field(default=True)
    char_scale: float = Field(default=0.3, ge=0.01, le=1.0)
    char_max_height_ratio: float = Field(default=0.82, ge=0.01, le=1.0)
    char_x_offset: int = Field(default=-10, ge=-5000, le=5000)
    char_y_offset: int = Field(default=0, ge=-5000, le=5000)


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

    min_summary_count: int = Field(
        default=10, ge=1, le=1000, description="最少总结条数"
    )
    max_summary_count: int = Field(
        default=200, ge=1, le=1000, description="最多总结条数"
    )
    fetch_batch_size: int = Field(default=50, ge=1, le=200, description="单次拉取条数")
    summary_default_count: int = Field(
        default=50, ge=1, le=200, description="LLM 未指定时的默认总结条数"
    )
    summary_planning_model: str = Field(
        default="deepseek-chat", description="总结规划阶段模型"
    )
    summary_planning_max_tokens: int = Field(
        default=800, ge=128, le=8192, description="总结规划阶段最大 tokens"
    )
    summary_planning_round_limit: int = Field(
        default=3, ge=1, le=6, description="总结规划工具循环上限"
    )
    summary_tool_scan_limit: int = Field(
        default=300, ge=10, le=500, description="总结工具本地扫描历史硬上限"
    )

    summary_model: str = Field(default="deepseek-chat", description="总结模型")
    summary_temperature: float = Field(
        default=0.4, ge=0.0, le=2.0, description="总结温度参数"
    )
    summary_max_tokens: int = Field(
        default=1200, ge=128, le=8192, description="总结最大 tokens"
    )

    layout_params: LayoutParamsSchema = Field(
        default_factory=LayoutParamsSchema,
        description="总结图片布局参数",
    )

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
