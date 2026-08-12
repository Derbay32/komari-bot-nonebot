"""Komari Chat 配置 Schema。

主动回复频控与回复履约 worker 的 11 个活字段从 ``komari_memory_config``
迁入本表（KOMARIBOT-7），死字段 ``proactive_score_threshold`` 随迁出
删除；回复履约另有独立的回复时效字段
``reply_fulfillment_freshness_seconds``（TSK-81）。TSK-87 contract 后
旧 ``reply_commit_*`` 配置名整体改名为 ``reply_fulfillment_*``，
新增 ``reply_fulfillment_retry_max_seconds`` 退避上限字段。
"""

from typing import ClassVar

from sqlalchemy import CheckConstraint

from komari_bot.config.typed_config import Field, TypedConfigModel, typed_model_config


class KomariChatConfigSchema(TypedConfigModel, table=True):
    """Komari Chat 插件配置。"""

    plugin_name: ClassVar[str] = "komari_chat"
    __tablename__ = "komari_chat_config"

    model_config = typed_model_config(
        json_schema_extra={"default_apply_mode": "immediate"},
    )

    # 时效字段的数据库级 CHECK 与 0007 迁移保持一致，避免 autogenerate 漂移
    __table_args__ = (
        CheckConstraint(
            "reply_fulfillment_freshness_seconds >= 30 "
            "AND reply_fulfillment_freshness_seconds <= 300",
            name="ck_komari_chat_config_reply_fulfillment_freshness",
        ),
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

    # 回复履约后台 worker
    reply_fulfillment_worker_interval_seconds: int = Field(
        default=5,
        ge=1,
        le=300,
        description="回复履约后台 worker 的扫描间隔（秒）",
    )
    reply_fulfillment_batch_size: int = Field(
        default=20,
        ge=1,
        le=200,
        description="回复履约单轮最大领取数",
    )
    reply_fulfillment_lease_seconds: int = Field(
        default=120,
        ge=30,
        le=900,
        description="回复履约 worker 租约时长（秒）",
    )
    reply_fulfillment_max_attempts: int = Field(
        default=20,
        ge=1,
        le=100,
        description="送达后承诺自动重试上限，耗尽后保留 FAILED 对账记录",
    )
    reply_fulfillment_retry_base_seconds: int = Field(
        default=5,
        ge=1,
        le=300,
        description="送达后承诺指数退避基准秒数",
    )
    reply_fulfillment_retry_max_seconds: int = Field(
        default=3600,
        ge=1,
        le=86_400,
        description="送达后承诺指数退避上限秒数",
    )
    reply_fulfillment_tombstone_retention_days: int = Field(
        default=30,
        ge=1,
        le=365,
        description="已完成或未送达的履约身份保护期天数",
    )
    reply_fulfillment_freshness_seconds: int = Field(
        default=120,
        ge=30,
        le=300,
        description="回复准备完成到开始发送的时效窗口（秒），满时效未发送即按未送达终止",
        json_schema_extra={"apply_mode": "immediate"},
    )
