"""Komari Context 配置 Schema。"""

from typing import ClassVar

from komari_bot.config.typed_config import Field, TypedConfigModel, typed_model_config


class KomariContextConfigSchema(TypedConfigModel, table=True):
    """最近上下文与聊天履约证据的 Redis 资源所有权配置。"""

    plugin_name: ClassVar[str] = "komari_context"
    __tablename__ = "komari_context_config"

    model_config = typed_model_config(
        json_schema_extra={"default_apply_mode": "immediate"},
    )

    redis_db: int = Field(
        default=1,
        description="Redis 数据库编号 (避免与其他插件冲突)",
        json_schema_extra={"apply_mode": "rebuild"},
    )


__all__ = ["KomariContextConfigSchema"]
