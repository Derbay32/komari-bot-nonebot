"""Sentry 初始化与事件过滤辅助函数。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

from nonebot.exception import (
    FinishedException,
    PausedException,
    RejectedException,
    StopPropagation,
)

_IGNORED_EXCEPTION_TYPES = (
    StopPropagation,
    PausedException,
    RejectedException,
    FinishedException,
)


class SentryConfigProtocol(Protocol):
    """Sentry 初始化需要的最小配置接口。"""

    environment: str
    release: str
    debug: bool
    error_sample_rate: float
    traces_sample_rate: float
    profiles_sample_rate: float
    attach_stacktrace: bool
    send_default_pii: bool
    max_breadcrumbs: int
    breadcrumb_level: str
    event_level: str


def get_ignored_sentry_exceptions() -> tuple[type[BaseException], ...]:
    """返回默认忽略的 NoneBot 控制流异常。"""
    return _IGNORED_EXCEPTION_TYPES


def should_ignore_sentry_exception(error: BaseException) -> bool:
    """判断异常是否应被 Sentry 忽略。"""
    return isinstance(error, get_ignored_sentry_exceptions())


def sentry_before_send(
    event: dict[str, Any],
    hint: dict[str, Any],
) -> dict[str, Any] | None:
    """在发送前丢弃框架控制流异常。"""
    exc_info = hint.get("exc_info")
    if not isinstance(exc_info, tuple) or len(exc_info) < 2:
        return event

    error = exc_info[1]
    if isinstance(error, BaseException) and should_ignore_sentry_exception(error):
        return None
    return event


def build_sentry_init_options(
    *,
    config: SentryConfigProtocol,
    dsn: str,
    resolve_level: Callable[[str, int], int],
    logging_integration_factory: Callable[..., Any],
    asyncio_integration_factory: Callable[[], Any],
    fastapi_integration_factory: Callable[[], Any],
    starlette_integration_factory: Callable[[], Any],
    environ: Mapping[str, str],
) -> dict[str, Any]:
    """构建 sentry_sdk.init 所需参数。"""
    breadcrumb_level = resolve_level(config.breadcrumb_level, logging.INFO)
    event_level = resolve_level(config.event_level, logging.ERROR)
    environment = config.environment.strip() or environ.get("ENVIRONMENT", "prod")
    release = config.release.strip() or None

    return {
        "dsn": dsn,
        "environment": environment,
        "release": release,
        "debug": config.debug,
        "sample_rate": config.error_sample_rate,
        "traces_sample_rate": config.traces_sample_rate,
        "profiles_sample_rate": config.profiles_sample_rate,
        "attach_stacktrace": config.attach_stacktrace,
        "send_default_pii": config.send_default_pii,
        "max_breadcrumbs": config.max_breadcrumbs,
        "before_send": sentry_before_send,
        "ignore_errors": list(get_ignored_sentry_exceptions()),
        "integrations": [
            logging_integration_factory(
                level=breadcrumb_level,
                event_level=event_level,
            ),
            asyncio_integration_factory(),
            fastapi_integration_factory(),
            starlette_integration_factory(),
        ],
    }
