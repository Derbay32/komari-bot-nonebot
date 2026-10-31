"""Komari Memory 配置 Schema（Hindsight 调用侧）。"""

from typing import ClassVar

from komari_bot.config.typed_config import Field, TypedConfigModel, typed_model_config


class KomariMemoryConfigSchema(TypedConfigModel, table=True):
    """Komari Memory 插件配置。"""

    plugin_name: ClassVar[str] = "komari_memory"
    __tablename__ = "komari_memory_config"
    startup_required: ClassVar[bool] = False

    model_config = typed_model_config(
        json_schema_extra={"default_apply_mode": "immediate"},
    )

    # TSK-193：Hindsight 共享长期记忆调用侧连接配置。
    # 新任务会话在开始时读取一次快照；已有会话保留其初始设置。
    hindsight_base_url: str = Field(
        default="",
        description="Hindsight 服务基础地址（由运维提供）",
        json_schema_extra={"apply_mode": "immediate"},
    )
    hindsight_api_key: str = Field(
        default="",
        description="Hindsight 服务凭据",
        json_schema_extra={"apply_mode": "immediate", "secret": True},
    )
    hindsight_bank_id: str = Field(
        default="",
        description="Hindsight 共享记忆库标识（不按群或用户分区）",
        json_schema_extra={"apply_mode": "immediate"},
    )
    hindsight_request_timeout_seconds: float = Field(
        default=10.0,
        gt=0.0,
        le=120.0,
        description="Hindsight 单次请求超时（秒）",
        json_schema_extra={"apply_mode": "immediate"},
    )
    hindsight_recovery_interval_seconds: float = Field(
        default=30.0,
        gt=0.0,
        le=3600.0,
        description="Hindsight 失败后的恢复探测最小间隔（秒）",
        json_schema_extra={"apply_mode": "immediate"},
    )


__all__ = ["KomariMemoryConfigSchema"]
