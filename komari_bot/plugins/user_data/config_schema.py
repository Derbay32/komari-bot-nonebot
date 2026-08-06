"""用户数据插件动态配置 Schema。"""

from typing import ClassVar

from komari_bot.common.typed_config import Field, TypedConfigModel, typed_model_config


class DynamicConfigSchema(TypedConfigModel, table=True):
    """用户数据插件配置（由 config_manager 管理）。"""

    plugin_name: ClassVar[str] = "user_data"
    __tablename__ = "komari_user_data_config"

    model_config = typed_model_config(
        json_schema_extra={"default_apply_mode": "restart"},
    )

    plugin_enable: bool = Field(
        default=True,
        description="插件启用状态",
        json_schema_extra={"apply_mode": "immediate"},
    )
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
        json_schema_extra={"apply_mode": "immediate"},
    )
