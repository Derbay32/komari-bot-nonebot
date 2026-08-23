"""Komari Knowledge 常识库插件配置 Schema。

用于管理 PostgreSQL 数据库连接和检索参数配置。
"""

from typing import ClassVar

from sqlalchemy.dialects.postgresql import JSONB

from komari_bot.config.typed_config import Field, TypedConfigModel, typed_model_config


class DynamicConfigSchema(TypedConfigModel, table=True):
    """
    Komari Knowledge 配置 Schema。
    """

    plugin_name: ClassVar[str] = "komari_knowledge"
    __tablename__ = "komari_knowledge_config"

    model_config = typed_model_config(
        json_schema_extra={"default_apply_mode": "immediate"},
    )

    # 插件控制
    plugin_enable: bool = Field(default=False, description="插件启用状态")

    # 白名单配置
    similarity_threshold: float = Field(
        default=0.65,
        ge=0.0,
        le=1.0,
        description="向量相似度阈值，低于此值的结果将被过滤",
    )

    # 检索配置
    query_rewrite_rules: dict[str, str] = Field(
        default={"你": "小鞠", "您的": "小鞠的"},
        sa_type=JSONB,
        description="查询重写规则，key 为待替换词，value 为替换词",
    )
    layer1_limit: int = Field(
        default=3, ge=0, le=10, description="Layer 1 关键词匹配最大返回数量"
    )
    layer2_limit: int = Field(
        default=2, ge=0, le=10, description="Layer 2 向量检索最大返回数量"
    )
    total_limit: int = Field(default=5, ge=1, le=20, description="总返回结果数量上限")
