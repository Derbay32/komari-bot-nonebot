"""Komari Context 配置 Schema。"""

from typing import ClassVar

from komari_bot.config.typed_config import Field, TypedConfigModel, typed_model_config


class KomariContextConfigSchema(TypedConfigModel, table=True):
    """最近上下文 Redis 资源与共享会话策略配置。"""

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

    # Shared conversational identity and failure-notification policy.
    bot_nickname: str = Field(default="小鞠知花", description="机器人昵称")
    error_notify_enabled: bool = Field(
        default=True,
        json_schema_extra={"apply_mode": "immediate"},
        description="回复生成失败时是否向 SUPERUSER 私聊发送极简诊断通知（群内错误提示不受此开关影响）",
    )

    # Still-active recent interaction-buffer controls.
    global_interaction_enabled: bool = Field(
        default=True,
        description="是否启用跨群互动事件缓冲",
    )
    global_interaction_trigger_size: int = Field(
        default=20,
        ge=5,
        le=200,
        description="触发事件总结的 Redis 缓冲条数阈值，不是 LIST 最大长度",
    )


__all__ = ["KomariContextConfigSchema"]
