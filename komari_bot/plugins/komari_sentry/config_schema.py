"""Komari Sentry 插件配置 Schema。"""

from __future__ import annotations

from typing import ClassVar

from pydantic import field_validator

from komari_bot.config.typed_config import Field, TypedConfigModel, typed_model_config

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _normalize_log_level(value: object, default: str) -> str:
    """规范化日志级别字段。"""
    text = str(value or "").strip().upper()
    if text not in _VALID_LOG_LEVELS:
        return default
    return text


class KomariSentryConfigSchema(TypedConfigModel, table=True):
    """Komari Sentry 动态配置。"""

    plugin_name: ClassVar[str] = "komari_sentry"
    __tablename__ = "komari_sentry_config"

    model_config = typed_model_config(
        json_schema_extra={"default_apply_mode": "restart"},
    )

    plugin_enable: bool = Field(default=False, description="是否启用 Sentry 插件")
    dsn: str = Field(
        default="",
        description="Sentry DSN（为空时不会初始化）",
        json_schema_extra={"secret": True},
    )
    environment: str = Field(
        default="",
        description="Sentry environment（为空时回退 ENVIRONMENT 环境变量）",
    )
    release: str = Field(
        default="",
        description="Sentry release（可选）",
    )
    debug: bool = Field(default=False, description="是否启用 Sentry SDK debug 日志")

    error_sample_rate: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="错误事件采样率（sample_rate）",
    )
    traces_sample_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="事务追踪采样率（traces_sample_rate）",
    )
    profiles_sample_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="性能 profile 采样率（profiles_sample_rate）",
    )
    attach_stacktrace: bool = Field(default=True, description="是否附加堆栈")
    send_default_pii: bool = Field(
        default=False,
        description=(
            "是否向 Sentry 上报 user 上下文（用户标识）；"
            "其余诊断数据默认全量上报并仅脱敏凭据"
        ),
    )
    max_breadcrumbs: int = Field(
        default=100,
        ge=0,
        le=2000,
        description="最大 breadcrumb 数量",
    )
    shutdown_timeout: float = Field(
        default=2.0,
        ge=0.0,
        le=30.0,
        description="关闭阶段 flush 超时秒数",
    )

    breadcrumb_level: str = Field(
        default="WARNING",
        description="记录为 breadcrumb 的日志级别",
    )
    sentry_logs_level: str = Field(
        default="WARNING",
        description="发送到 Sentry Logs 的日志级别",
    )
    event_level: str = Field(
        default="ERROR",
        description="转为 Sentry 事件的日志级别",
    )

    @field_validator("breadcrumb_level", mode="before")
    @classmethod
    def normalize_breadcrumb_level(cls, value: object) -> str:
        """规范化 breadcrumb 日志级别字段。"""
        return _normalize_log_level(value, "WARNING")

    @field_validator("sentry_logs_level", mode="before")
    @classmethod
    def normalize_sentry_logs_level(cls, value: object) -> str:
        """规范化 Sentry Logs 日志级别字段。"""
        return _normalize_log_level(value, "WARNING")

    @field_validator("event_level", mode="before")
    @classmethod
    def normalize_event_level(cls, value: object) -> str:
        """规范化 event 日志级别字段。"""
        return _normalize_log_level(value, "ERROR")
