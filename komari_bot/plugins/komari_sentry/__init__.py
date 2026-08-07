"""Komari Sentry - 统一的 Sentry 初始化与上报插件。"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any, Literal

from nonebot import get_driver, logger
from nonebot.plugin import PluginMetadata

if TYPE_CHECKING:
    from pydantic import BaseModel

from komari_bot.core.sentry_support import (
    build_sentry_init_options,
    ensure_sentry_privacy_hooks,
    set_sensitive_value_collector,
)

from .config_interface import get_config, get_config_async
from .config_schema import KomariSentryConfigSchema

try:
    import sentry_sdk
    from sentry_sdk.integrations.asyncio import AsyncioIntegration
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration
    from sentry_sdk.integrations.loguru import LoguruIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration
except ImportError:
    sentry_sdk = None  # type: ignore[assignment]
    AsyncioIntegration = None  # type: ignore[assignment]
    FastApiIntegration = None  # type: ignore[assignment]
    LoggingIntegration = None  # type: ignore[assignment]
    LoguruIntegration = None  # type: ignore[assignment]
    StarletteIntegration = None  # type: ignore[assignment]

__plugin_meta__ = PluginMetadata(
    name="komari_sentry",
    description="Sentry 连接与事件上报插件",
    usage="自动初始化，无需命令",
    config=KomariSentryConfigSchema,
)

driver = get_driver()
_initialized_by_plugin = False


def _resolve_level(level_name: str, default: int) -> int:
    """将日志级别字符串转换为 logging 常量。"""
    value = getattr(logging, level_name.upper(), None)
    if isinstance(value, int):
        return value
    return default


def _resolve_dsn(config: KomariSentryConfigSchema) -> str:
    """优先读取插件配置，未配置时回退环境变量。"""
    dsn = config.dsn.strip()
    if dsn:
        return dsn
    return os.getenv("SENTRY_DSN", "").strip()


def _collect_string_values(value: object, values: list[str]) -> None:
    """递归收集字符串值（覆盖嵌套 list/dict 结构）。"""
    if isinstance(value, str):
        values.append(value)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_string_values(item, values)
    elif isinstance(value, dict):
        for item in value.values():
            _collect_string_values(item, values)


def _extract_secret_values(config: BaseModel, values: list[str]) -> None:
    """按 model_fields 的 secret 标记收集字段值。"""
    for field_name, field_info in config.__class__.model_fields.items():
        field_extra = field_info.json_schema_extra
        if not isinstance(field_extra, dict) or field_extra.get("secret") is not True:
            continue
        _collect_string_values(getattr(config, field_name, None), values)


def _collect_config_secret_values() -> list[str]:
    """遍历全部已注册配置管理器，收集标记为 secret 的字段值。

    整体 try/except 防御：manager 未初始化、config_manager 不可用等
    异常均降级为空集，由 sentry_support 的累计集合兜底。
    """
    values: list[str] = []
    try:
        from komari_bot.plugins.config_manager import get_registered_config_managers

        for manager in get_registered_config_managers().values():
            try:
                config = manager.get()
            except Exception:
                logger.debug("[KomariSentry] 读取配置快照失败", exc_info=True)
                continue
            _extract_secret_values(config, values)
    except Exception:
        logger.debug("[KomariSentry] 收集配置秘密值异常", exc_info=True)
    return values


@driver.on_startup
async def startup() -> None:
    """初始化 Sentry SDK。"""
    global _initialized_by_plugin  # noqa: PLW0603

    config = await get_config_async()
    if not config.plugin_enable:
        logger.info("[KomariSentry] 插件未启用，跳过初始化")
        return

    if sentry_sdk is None:
        logger.warning("[KomariSentry] sentry_sdk 未安装，跳过初始化")
        return
    if (
        AsyncioIntegration is None
        or FastApiIntegration is None
        or LoggingIntegration is None
        or LoguruIntegration is None
        or StarletteIntegration is None
    ):
        logger.warning("[KomariSentry] sentry_sdk integrations 不可用，跳过初始化")
        return

    dsn = _resolve_dsn(config)
    if not dsn:
        logger.warning("[KomariSentry] DSN 为空，跳过初始化")
        return

    if sentry_sdk.is_initialized():
        client = sentry_sdk.get_client()
        try:
            ensure_sentry_privacy_hooks(
                client,
                allow_user_context=config.send_default_pii,
            )
        except Exception:
            logger.critical(
                "[KomariSentry] 外部 Sentry Client 无法安装或验证隐私钩子，拒绝继续启动"
            )
            raise
        logger.info(
            "[KomariSentry] 检测到外部 Sentry Client，已合并并验证项目隐私钩子"
        )
        set_sensitive_value_collector(_collect_config_secret_values)
        return

    init_options = build_sentry_init_options(
        config=config,
        dsn=dsn,
        resolve_level=_resolve_level,
        logging_integration_factory=LoggingIntegration,
        loguru_integration_factory=LoguruIntegration,
        asyncio_integration_factory=AsyncioIntegration,
        fastapi_integration_factory=FastApiIntegration,
        starlette_integration_factory=StarletteIntegration,
        environ=os.environ,
    )
    sentry_sdk.init(**init_options)
    client = sentry_sdk.get_client()
    try:
        ensure_sentry_privacy_hooks(
            client,
            allow_user_context=config.send_default_pii,
        )
    except Exception:
        client.close(timeout=0)
        logger.critical("[KomariSentry] 初始化后隐私钩子验证失败，已关闭 Client")
        raise
    _initialized_by_plugin = True
    set_sensitive_value_collector(_collect_config_secret_values)
    logger.info(
        "[KomariSentry] 初始化完成 env={} traces={:.3f} profiles={:.3f}",
        init_options["environment"],
        config.traces_sample_rate,
        config.profiles_sample_rate,
    )


@driver.on_shutdown
async def shutdown() -> None:
    """关闭阶段 flush Sentry 事件。"""
    if sentry_sdk is None or not sentry_sdk.is_initialized():
        return

    timeout = get_config().shutdown_timeout
    try:
        sentry_sdk.flush(timeout=timeout)
    except Exception:
        logger.exception("[KomariSentry] flush 失败")
    else:
        if _initialized_by_plugin:
            logger.info("[KomariSentry] flush 完成")


def is_initialized() -> bool:
    """Sentry 是否已初始化。"""
    return sentry_sdk is not None and sentry_sdk.is_initialized()


def capture_exception(error: BaseException) -> str | None:
    """上报异常并返回事件 ID。"""
    if sentry_sdk is None:
        return None
    event_id = sentry_sdk.capture_exception(error)
    return str(event_id) if event_id else None


def capture_message(
    message: str,
    level: Literal["fatal", "critical", "error", "warning", "info", "debug"] = "info",
) -> str | None:
    """上报普通消息并返回事件 ID。"""
    if sentry_sdk is None:
        return None
    event_id = sentry_sdk.capture_message(message, level=level)
    return str(event_id) if event_id else None


def set_tag(key: str, value: str) -> None:
    """设置 Sentry tag。"""
    if sentry_sdk is None:
        return
    sentry_sdk.set_tag(key, value)


def set_user(user: dict[str, Any] | None) -> None:
    """设置 Sentry user 上下文。"""
    if sentry_sdk is None:
        return
    sentry_sdk.set_user(user)
