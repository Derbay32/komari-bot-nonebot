"""Agent Run 日志插件动态配置。"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from komari_bot.common.typed_config import Field, TypedConfigModel, typed_model_config


class AgentRunLoggerConfigSchema(TypedConfigModel, table=True):
    """Agent Run 日志配置（PostgreSQL 强类型表）。"""

    plugin_name: ClassVar[str] = "agent_run_logger"
    __tablename__ = "komari_agent_run_logger_config"

    model_config = typed_model_config(
        extra="forbid",
        json_schema_extra={"default_apply_mode": "immediate"},
    )

    log_enabled: bool = Field(default=True, description="是否持久化 Agent Run 日志")
    retention_days: int = Field(
        default=1,
        ge=1,
        le=90,
        description="Agent Run 详细日志按日志日保留的天数",
    )


class AgentRunLoggerEnvConfigSchema(BaseModel):
    """从 NoneBot 全局环境中提取本插件字段时使用的宽松投影。"""

    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={"default_apply_mode": "immediate"},
    )

    log_enabled: bool = Field(default=True, description="是否持久化 Agent Run 日志")
    retention_days: int = Field(
        default=1,
        ge=1,
        le=90,
        description="Agent Run 详细日志按日志日保留的天数",
    )
