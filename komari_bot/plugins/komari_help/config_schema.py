"""Komari Help 帮助文档插件配置 Schema。"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import field_validator
from sqlalchemy.dialects.postgresql import JSONB

from komari_bot.common.typed_config import Field, TypedConfigModel, typed_model_config


class DynamicConfigSchema(TypedConfigModel, table=True):
    """Komari Help 配置 Schema。"""

    plugin_name: ClassVar[str] = "komari_help"
    __tablename__ = "komari_help_config"

    model_config = typed_model_config(
        json_schema_extra={"default_apply_mode": "immediate"},
    )

    plugin_enable: bool = Field(
        default=False,
        description="插件启用状态",
        json_schema_extra={"apply_mode": "restart"},
    )

    user_whitelist: list[str] = Field(
        default_factory=list, description="用户白名单，为空则允许所有用户", sa_type=JSONB
    )
    group_whitelist: list[str] = Field(
        default_factory=list, description="群聊白名单，为空则允许所有群聊", sa_type=JSONB
    )

    similarity_threshold: float = Field(
        default=0.60,
        ge=0.0,
        le=1.0,
        description="向量相似度阈值",
    )
    layer1_limit: int = Field(
        default=3, ge=0, le=10, description="关键词层最大返回数量"
    )
    layer2_limit: int = Field(default=2, ge=0, le=10, description="向量层最大返回数量")
    total_limit: int = Field(default=5, ge=1, le=20, description="总返回数量上限")
    default_result_limit: int = Field(
        default=3, ge=1, le=10, description="命令默认返回数量"
    )
    max_reply_result_count: int = Field(
        default=3,
        ge=1,
        le=10,
        description="检索回复中最多展示的帮助文档数量",
    )

    query_rewrite_rules: dict[str, str] = Field(
        default_factory=dict,
        sa_type=JSONB,
        description="查询重写规则",
    )
    auto_scan_on_startup: bool = Field(
        default=True,
        description="启动时是否自动扫描插件元数据",
        json_schema_extra={"apply_mode": "restart"},
    )
    disabled_auto_help_plugins: list[str] = Field(
        default_factory=list,
        sa_type=JSONB,
        description="禁止自动生成帮助文档的插件名列表",
    )
    show_category_emoji: bool = Field(
        default=True, description="展示时是否显示分类 emoji"
    )
    max_content_preview_length: int = Field(
        default=100,
        ge=20,
        le=500,
        description="内容预览最大长度",
    )

    @field_validator(
        "user_whitelist",
        "group_whitelist",
        "disabled_auto_help_plugins",
        mode="before",
    )
    @classmethod
    def parse_list_string(cls, value: Any) -> Any:
        if isinstance(value, str):
            import json

            try:
                parsed = json.loads(value)
                return [str(item) for item in parsed]
            except (json.JSONDecodeError, TypeError):
                return [item.strip() for item in value.split(",") if item.strip()]
        return value
