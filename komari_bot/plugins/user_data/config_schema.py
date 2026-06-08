"""用户数据插件动态配置 Schema。"""

from datetime import datetime

from pydantic import BaseModel, Field


class DynamicConfigSchema(BaseModel):
    """用户数据插件配置（由 config_manager 管理）。"""

    version: str = Field(default="2.0", description="配置架构版本")
    last_updated: str = Field(
        default_factory=lambda: datetime.now().astimezone().isoformat(),
        description="最后更新时间戳",
    )

    plugin_enable: bool = Field(default=True, description="插件启用状态")
    initial_favorability: int = Field(
        default=0,
        ge=0,
        le=400,
        description="新用户初始当前好感度",
    )
    max_favorability_delta_per_reply: int = Field(
        default=5,
        ge=1,
        le=20,
        description="单次回复允许记录的最大好感度变化绝对值",
    )
