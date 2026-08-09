"""Komari Chat 配置 Schema。

主动回复频控与回复送达副作用 outbox 的 10 个活字段从
``komari_memory_config`` 迁入本表（KOMARIBOT-7），死字段
``proactive_score_threshold`` 随迁出删除。
"""

from typing import ClassVar

from komari_bot.config.typed_config import Field, TypedConfigModel, typed_model_config


class KomariChatConfigSchema(TypedConfigModel, table=True):
    """Komari Chat 插件配置。"""

    plugin_name: ClassVar[str] = "komari_chat"
    __tablename__ = "komari_chat_config"

    model_config = typed_model_config(
        json_schema_extra={"default_apply_mode": "immediate"},
    )

    # 主动回复配置
    proactive_enabled: bool = Field(
        default=False,
        description="是否启用主动回复",
        json_schema_extra={"apply_mode": "immediate"},
    )
    proactive_cooldown: int = Field(
        default=300,
        ge=5,
        le=3600,
        description="主动回复送达后的冷却时间（秒）",
        json_schema_extra={"apply_mode": "immediate"},
    )
    proactive_max_per_hour: int = Field(
        default=400,
        ge=1,
        le=800,
        description="最近一小时最大主动回复次数（包含生成中的预占）",
        json_schema_extra={"apply_mode": "immediate"},
    )
    proactive_reservation_ttl_seconds: int = Field(
        default=360,
        ge=30,
        le=900,
        description="主动回复生成与发送阶段的 Redis 预占有效期（秒）",
        json_schema_extra={"apply_mode": "immediate"},
    )

    # 回复送达后副作用 outbox
    reply_commit_worker_interval_seconds: int = Field(
        default=5,
        ge=1,
        le=300,
        description="聊天回复副作用 outbox 的后台扫描间隔（秒）",
    )
    reply_commit_batch_size: int = Field(
        default=20,
        ge=1,
        le=200,
        description="聊天回复副作用 outbox 单轮最大领取数",
    )
    reply_commit_lease_seconds: int = Field(
        default=120,
        ge=30,
        le=900,
        description="聊天回复副作用 outbox worker 租约时长（秒）",
    )
    reply_commit_max_attempts: int = Field(
        default=20,
        ge=1,
        le=100,
        description="聊天回复副作用 outbox 自动重试上限，耗尽后保留 FAILED 对账记录",
    )
    reply_commit_retry_base_seconds: int = Field(
        default=5,
        ge=1,
        le=300,
        description="聊天回复副作用 outbox 指数退避基准秒数",
    )
    reply_commit_tombstone_retention_days: int = Field(
        default=30,
        ge=1,
        le=365,
        description="已完成或取消的聊天 operation 防重记录保留天数",
    )
