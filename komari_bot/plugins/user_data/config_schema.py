"""用户数据插件动态配置 Schema。"""

from datetime import datetime

from pydantic import BaseModel, Field


class DynamicConfigSchema(BaseModel):
    """用户数据插件配置（由 config_manager 管理）。"""

    version: str = Field(default="1.0", description="配置架构版本")
    last_updated: str = Field(
        default_factory=lambda: datetime.now().astimezone().isoformat(),
        description="最后更新时间戳",
    )

    plugin_enable: bool = Field(default=True, description="插件启用状态")

    data_retention_days: int = Field(
        default=30, ge=1, le=3650, description="数据保留天数"
    )
