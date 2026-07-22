"""Agent Run 日志插件动态配置。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AgentRunLoggerConfigSchema(BaseModel):
    """Agent Run 日志配置。"""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"default_apply_mode": "immediate"},
    )

    version: str = Field(default="1.0", description="配置架构版本")
    last_updated: str = Field(
        default_factory=lambda: datetime.now().astimezone().isoformat(),
        description="最后更新时间戳",
    )
    log_enabled: bool = Field(default=True, description="是否持久化 Agent Run 日志")
    retention_days: int = Field(
        default=1,
        ge=1,
        le=90,
        description="Agent Run 详细日志按日志日保留的天数",
    )
