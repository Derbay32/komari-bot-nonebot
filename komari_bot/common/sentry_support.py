"""Sentry 初始化与事件过滤辅助函数。"""

from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol, cast

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

_SAFE_TELEMETRY_ATTRIBUTE_KEYS = frozenset(
    {
        "code.function.name",
        "code.line.number",
        "http.method",
        "http.response.status_code",
        "logger.name",
        "method",
        "sentry.origin",
        "status_code",
    }
)
_SAFE_EVENT_TAG_KEYS = frozenset(
    {"component", "operation", "phase", "plugin", "service", "status"}
)
_SAFE_CONTEXT_FIELDS = {
    "app": frozenset(
        {
            "app_build",
            "app_identifier",
            "app_name",
            "app_start_time",
            "app_version",
            "build_type",
            "in_foreground",
        }
    ),
    "runtime": frozenset({"build", "name", "version"}),
    "trace": frozenset(
        {"op", "origin", "parent_span_id", "span_id", "status", "trace_id"}
    ),
}


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
    sentry_logs_level: str
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
    *,
    allow_user_context: bool = False,
) -> dict[str, Any] | None:
    """在发送前丢弃控制流异常并清除业务内容与用户标识。"""
    exc_info = hint.get("exc_info")
    if isinstance(exc_info, tuple) and len(exc_info) >= 2:
        error = exc_info[1]
        if isinstance(error, BaseException) and should_ignore_sentry_exception(error):
            return None

    _sanitize_event(event, allow_user_context=allow_user_context)
    return event


def sentry_before_breadcrumb(
    breadcrumb: dict[str, Any],
    _hint: dict[str, Any],
) -> dict[str, Any]:
    """隐藏 breadcrumb 正文，仅保留低敏诊断元数据。"""
    sanitized = {
        key: breadcrumb[key]
        for key in ("type", "category", "level", "timestamp")
        if key in breadcrumb
    }
    if "message" in breadcrumb:
        sanitized["message"] = _redacted_text_summary(
            breadcrumb.get("message"),
            label="breadcrumb 正文",
        )

    safe_data = _safe_telemetry_attributes(breadcrumb.get("data"))
    if safe_data:
        sanitized["data"] = safe_data
    return sanitized


def sentry_before_send_log(
    log: dict[str, Any],
    _hint: dict[str, Any],
) -> dict[str, Any]:
    """隐藏 Sentry Logs 正文和插值参数。"""
    sanitized = {
        key: log[key]
        for key in (
            "severity_number",
            "severity_text",
            "time_unix_nano",
            "trace_id",
            "span_id",
        )
        if key in log and isinstance(log[key], (str, int, float, bool))
    }
    sanitized["body"] = _redacted_text_summary(
        log.get("body"),
        label="日志正文",
    )
    sanitized["attributes"] = _safe_telemetry_attributes(log.get("attributes"))
    return sanitized


def sentry_before_send_transaction(
    event: dict[str, Any],
    _hint: dict[str, Any],
    *,
    allow_user_context: bool = False,
) -> dict[str, Any]:
    """净化事务、span 与请求上下文，阻止 tracing 绕过错误事件钩子。"""
    _sanitize_event(event, allow_user_context=allow_user_context)

    if "transaction" in event:
        event["transaction"] = _redacted_text_summary(
            event.get("transaction"),
            label="事务名称",
        )

    transaction_info = event.get("transaction_info")
    if isinstance(transaction_info, dict):
        source = transaction_info.get("source")
        event["transaction_info"] = (
            {"source": source}
            if source in {"component", "custom", "route", "task", "url"}
            else {}
        )

    spans = event.get("spans")
    if isinstance(spans, list):
        event["spans"] = [
            _sanitize_transaction_span(span)
            for span in spans
            if isinstance(span, dict)
        ]

    event.pop("measurements", None)
    return event


def _redacted_text_summary(value: object, *, label: str) -> str:
    """以不可逆长度摘要替代可能包含用户内容的文本。"""
    if value is None:
        return f"[{label}已隐藏]"
    return f"[{label}已隐藏，字符数={len(str(value))}]"


def _safe_telemetry_attributes(value: object) -> dict[str, Any]:
    """只保留不会携带业务正文或用户标识的遥测属性。"""
    if not isinstance(value, dict):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if str(key) in _SAFE_TELEMETRY_ATTRIBUTE_KEYS
        and isinstance(item, (str, int, float, bool))
    }


def _sanitize_transaction_span(span: dict[str, Any]) -> dict[str, Any]:
    """仅保留 span 的低敏标识和状态，并摘要 description。"""
    sanitized = {
        key: span[key]
        for key in (
            "exclusive_time",
            "op",
            "origin",
            "parent_span_id",
            "span_id",
            "start_timestamp",
            "status",
            "timestamp",
            "trace_id",
        )
        if key in span and isinstance(span[key], (str, int, float, bool))
    }
    if "description" in span:
        sanitized["description"] = _redacted_text_summary(
            span.get("description"),
            label="span 描述",
        )
    safe_data = _safe_telemetry_attributes(span.get("data"))
    if safe_data:
        sanitized["data"] = safe_data
    tags = span.get("tags")
    if isinstance(tags, dict):
        safe_tags = {
            str(key): value
            for key, value in tags.items()
            if str(key) in _SAFE_EVENT_TAG_KEYS
            and isinstance(value, (str, int, float, bool))
        }
        if safe_tags:
            sanitized["tags"] = safe_tags
    return sanitized


def _sanitize_event(
    event: dict[str, Any],
    *,
    allow_user_context: bool,
) -> None:
    """就地清理错误事件中的日志、请求、用户与局部变量。"""
    if "message" in event:
        event["message"] = _redacted_text_summary(
            event.get("message"),
            label="事件消息",
        )

    logentry = event.get("logentry")
    if isinstance(logentry, dict):
        for key in ("message", "formatted"):
            if key in logentry:
                logentry[key] = _redacted_text_summary(
                    logentry.get(key),
                    label="日志正文",
                )
        logentry.pop("params", None)

    _sanitize_exception_values(event.get("exception"))
    _strip_stack_variables(event.get("threads"))
    _sanitize_event_breadcrumbs(event.get("breadcrumbs"))

    request = event.get("request")
    if isinstance(request, dict):
        method = request.get("method")
        event["request"] = (
            {"method": method.upper()}
            if isinstance(method, str) and method
            else {}
        )

    contexts = event.get("contexts")
    if isinstance(contexts, dict):
        event["contexts"] = {
            name: {
                key: value
                for key, value in context.items()
                if key in allowed_fields
                and isinstance(value, (str, int, float, bool))
            }
            for name, context in contexts.items()
            if (allowed_fields := _SAFE_CONTEXT_FIELDS.get(name)) is not None
            and isinstance(context, dict)
        }

    tags = event.get("tags")
    if isinstance(tags, dict):
        event["tags"] = {
            str(key): value
            for key, value in tags.items()
            if str(key) in _SAFE_EVENT_TAG_KEYS
            and isinstance(value, (str, int, float, bool))
        }

    for key in ("extra", "server_name"):
        event.pop(key, None)
    if not allow_user_context:
        event.pop("user", None)


def _sanitize_exception_values(exception: object) -> None:
    """隐藏异常正文，同时保留异常类型和无局部变量的堆栈。"""
    if not isinstance(exception, dict):
        return
    values = exception.get("values")
    if not isinstance(values, list):
        return
    for value in values:
        if not isinstance(value, dict):
            continue
        if "value" in value:
            value["value"] = _redacted_text_summary(
                value.get("value"),
                label="异常正文",
            )
        _strip_stack_variables(value.get("stacktrace"))


def _strip_stack_variables(stacktrace_container: object) -> None:
    """移除异常与线程堆栈帧中的局部变量快照。"""
    if not isinstance(stacktrace_container, dict):
        return

    frames = stacktrace_container.get("frames")
    if isinstance(frames, list):
        for frame in frames:
            if isinstance(frame, dict):
                frame.pop("vars", None)

    values = stacktrace_container.get("values")
    if isinstance(values, list):
        for value in values:
            if isinstance(value, dict):
                _strip_stack_variables(value.get("stacktrace"))


def _sanitize_event_breadcrumbs(container: object) -> None:
    """对事件中已经收集的 breadcrumb 再执行一次净化。"""
    if not isinstance(container, dict):
        return
    values = container.get("values")
    if not isinstance(values, list):
        return
    container["values"] = [
        sentry_before_breadcrumb(item, {})
        for item in values
        if isinstance(item, dict)
    ]


def ensure_sentry_privacy_hooks(
    client: object,
    *,
    allow_user_context: bool,
) -> None:
    """为已经初始化的 Sentry Client 合并并验证项目隐私钩子。"""
    options = getattr(client, "options", None)
    if not isinstance(options, dict):
        message = "Sentry Client 未公开可验证的 options，无法安装隐私钩子"
        raise TypeError(message)

    sanitizers = {
        "before_send": partial(
            sentry_before_send,
            allow_user_context=allow_user_context,
        ),
        "before_breadcrumb": sentry_before_breadcrumb,
        "before_send_transaction": partial(
            sentry_before_send_transaction,
            allow_user_context=allow_user_context,
        ),
        "before_send_log": sentry_before_send_log,
    }
    for option_name, sanitizer in sanitizers.items():
        existing = options.get(option_name)
        if _contains_komari_privacy_hook(existing, option_name, sanitizer):
            continue
        options[option_name] = _compose_sentry_privacy_hook(
            existing,
            sanitizer,
            option_name=option_name,
        )

    ignored = options.get("ignore_errors")
    ignored_types = list(ignored) if isinstance(ignored, (list, tuple)) else []
    for exception_type in get_ignored_sentry_exceptions():
        if exception_type not in ignored_types:
            ignored_types.append(exception_type)
    options["ignore_errors"] = ignored_types

    missing = [
        option_name
        for option_name, sanitizer in sanitizers.items()
        if not _contains_komari_privacy_hook(
            options.get(option_name),
            option_name,
            sanitizer,
        )
    ]
    if missing:
        message = f"Sentry 隐私钩子安装验证失败: {', '.join(missing)}"
        raise RuntimeError(message)


def _contains_komari_privacy_hook(
    hook: object,
    option_name: str,
    sanitizer: object,
) -> bool:
    if getattr(hook, "__komari_privacy_hook__", None) == option_name:
        return True
    hook_function = getattr(hook, "func", hook)
    sanitizer_function = getattr(sanitizer, "func", sanitizer)
    return hook_function is sanitizer_function


def _compose_sentry_privacy_hook(
    existing: object,
    sanitizer: object,
    *,
    option_name: str,
) -> object:
    sanitizer_hook = cast(
        "Callable[[dict[str, Any], dict[str, Any]], dict[str, Any] | None]",
        sanitizer,
    )
    existing_hook = (
        cast(
            "Callable[[dict[str, Any], dict[str, Any]], dict[str, Any] | None]",
            existing,
        )
        if callable(existing)
        else None
    )

    def _combined(
        payload: dict[str, Any],
        hint: dict[str, Any],
    ) -> dict[str, Any] | None:
        current = payload
        if existing_hook is not None:
            try:
                existing_result = existing_hook(current, hint)
            except Exception:
                # 外部钩子异常时故障关闭，绝不把未经净化的 payload 继续发送。
                return None
            if not isinstance(existing_result, dict):
                return None
            current = existing_result
        return sanitizer_hook(current, hint)

    _combined.__komari_privacy_hook__ = option_name  # type: ignore[attr-defined]
    return _combined


def build_sentry_init_options(
    *,
    config: SentryConfigProtocol,
    dsn: str,
    resolve_level: Callable[[str, int], int],
    logging_integration_factory: Callable[..., Any],
    loguru_integration_factory: Callable[..., Any],
    asyncio_integration_factory: Callable[[], Any],
    fastapi_integration_factory: Callable[[], Any],
    starlette_integration_factory: Callable[[], Any],
    environ: Mapping[str, str],
) -> dict[str, Any]:
    """构建 sentry_sdk.init 所需参数。"""
    breadcrumb_level = resolve_level(config.breadcrumb_level, logging.WARNING)
    sentry_logs_level = resolve_level(config.sentry_logs_level, logging.WARNING)
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
        "enable_logs": True,
        "before_send": partial(
            sentry_before_send,
            allow_user_context=config.send_default_pii,
        ),
        "before_breadcrumb": sentry_before_breadcrumb,
        "before_send_transaction": partial(
            sentry_before_send_transaction,
            allow_user_context=config.send_default_pii,
        ),
        "before_send_log": sentry_before_send_log,
        "ignore_errors": list(get_ignored_sentry_exceptions()),
        "integrations": [
            logging_integration_factory(
                sentry_logs_level=sentry_logs_level,
                level=breadcrumb_level,
                event_level=event_level,
            ),
            loguru_integration_factory(
                sentry_logs_level=sentry_logs_level,
                level=breadcrumb_level,
                event_level=event_level,
            ),
            asyncio_integration_factory(),
            fastapi_integration_factory(),
            starlette_integration_factory(),
        ],
    }
