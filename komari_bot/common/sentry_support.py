"""Sentry 初始化与事件过滤辅助函数。

采用黑名单式脱敏：全量保留诊断数据，仅隐藏凭据类极度敏感字段。

三层凭据识别（纵深防御）：
1. 字段名黑名单：递归扫描 payload，key 归一化后精确匹配黑名单，命中整值替换。
2. 值模式正则：仅覆盖确定形状的凭据（sk- key、Bearer token、连接串 userinfo、DSN、API 形状 URL）。
3. 精确值替换：收集已配置的真实秘密值，在全 payload 字符串内做字面量子串替换。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

from nonebot.exception import (
    FinishedException,
    PausedException,
    RejectedException,
    StopPropagation,
)

logger = logging.getLogger(__name__)

_IGNORED_EXCEPTION_TYPES = (
    StopPropagation,
    PausedException,
    RejectedException,
    FinishedException,
)

_FILTERED = "[Filtered]"
_MIN_SENSITIVE_VALUE_LENGTH = 8

# 字段名黑名单（归一化形态：小写并去除 - 和 _）
_SENSITIVE_KEY_NAMES = frozenset(
    {
        "authorization",
        "proxyauthorization",
        "xapikey",
        "apikey",
        "token",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "clientsecret",
        "secret",
        "password",
        "passwd",
        "pwd",
        "cookie",
        "setcookie",
        "session",
        "privatekey",
        "dsn",
    }
)

# 值模式正则：仅覆盖确定形状，不做通用长 token 猜测
_VALUE_PATTERNS = (
    # OpenAI 兼容 key（sk- 开头的长串）
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    # Bearer token 头部值
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.+/=]{16,}", re.IGNORECASE),
    # 连接串 userinfo（scheme://user:pass@host，覆盖 postgres/redis 等）
    re.compile(r"(?<=://)[^\s/?#:@]+:[^\s/?#:@]+(?=@)"),
    # Sentry DSN 形状（https://<key>@host/...）
    re.compile(r"https?://[A-Za-z0-9]+@[A-Za-z0-9.\-]+(?::\d+)?/[^\s\"']*"),
    # API 形状 URL（scheme://host/v数字[字母][/...]，如 /v1、/v1beta）
    re.compile(
        r"[a-z][a-z0-9+.\-]*://[A-Za-z0-9.\-]+(?::\d+)?/v\d+[a-z]*(?:/[^\s\"']*)?",
        re.IGNORECASE,
    ),
)

# 精确值替换：进程内累计集合（只增不减，轮换掉的旧 key 继续受保护）
_registered_sensitive_values: set[str] = set()
_sensitive_values_lock = threading.RLock()
_sensitive_value_collector: Callable[[], Iterable[str]] | None = None

# collector 重跑门控：短 TTL 缓存，避免高频钩子反复遍历配置管理器；
# 以 collector 对象身份为缓存键，collector 更换后立即重跑。
_COLLECTOR_CACHE_TTL_SECONDS = 30.0
_cached_collector: object | None = None
_collector_refresh_at: float = 0.0


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


def register_sensitive_value(value: str | None) -> None:
    """登记需要精确替换的敏感值（累计集合，只增不减）。

    小于 8 字符的值不入清单，防止误伤短字符串。
    """
    if value is None:
        return
    stripped = value.strip()
    if len(stripped) < _MIN_SENSITIVE_VALUE_LENGTH:
        return
    with _sensitive_values_lock:
        _registered_sensitive_values.add(stripped)


def set_sensitive_value_collector(
    collector: Callable[[], Iterable[str]] | None,
) -> None:
    """注入当前配置秘密收集器（由插件层在启动时调用）。

    sentry_support 属 common 层，禁止直接依赖插件层；
    通过依赖注入由 komari_sentry 插件注册遍历 config_manager 的 collector。
    """
    global _sensitive_value_collector  # noqa: PLW0603
    _sensitive_value_collector = collector


def _collect_sensitive_values() -> frozenset[str]:
    """收集当前配置秘密并与进程内累计集合取并集。

    collector 受短 TTL 缓存门控，高频钩子不会反复遍历配置管理器；
    累计集合只增不减，缓存窗口内轮换的秘密仍受保护，无正确性损失。
    收集动作本身异常时降级为仅用累计集合，不阻断事件。
    """
    global _cached_collector, _collector_refresh_at  # noqa: PLW0603
    now = time.monotonic()
    collector = _sensitive_value_collector
    if collector is not _cached_collector or now >= _collector_refresh_at:
        _cached_collector = collector
        _collector_refresh_at = now + _COLLECTOR_CACHE_TTL_SECONDS
        collected: set[str] = set()
        if collector is not None:
            try:
                for value in collector():
                    if isinstance(value, str):
                        stripped = value.strip()
                        if len(stripped) >= _MIN_SENSITIVE_VALUE_LENGTH:
                            collected.add(stripped)
            except Exception:
                logger.debug(
                    "Sentry 敏感值收集器执行失败，降级为累计集合",
                    exc_info=True,
                )
        with _sensitive_values_lock:
            _registered_sensitive_values.update(collected)
    with _sensitive_values_lock:
        return frozenset(_registered_sensitive_values)


def _is_sensitive_key(key: str) -> bool:
    """判断字段名是否命中黑名单。

    按点号分段后逐段归一化（小写并去除 - 和 _），
    覆盖 Sentry 集成常见的点号键约定（如 http.request.header.authorization）。
    """
    return any(
        segment.replace("-", "").replace("_", "") in _SENSITIVE_KEY_NAMES
        for segment in key.lower().split(".")
        if segment
    )


def _scrub_string(value: str, secrets: frozenset[str]) -> str:
    """对字符串先做精确值替换，再做正则形状替换。"""
    result = value
    for secret in secrets:
        if secret in result:
            result = result.replace(secret, _FILTERED)
    for pattern in _VALUE_PATTERNS:
        result = pattern.sub(_FILTERED, result)
    return result


def _scrub_payload(obj: Any, secrets: frozenset[str]) -> Any:
    """递归净化 payload：字段名黑名单 → 精确值替换 → 正则形状替换。"""
    if isinstance(obj, dict):
        scrubbed: dict[Any, Any] = {}
        for key, value in obj.items():
            if isinstance(key, str) and _is_sensitive_key(key):
                scrubbed[key] = _FILTERED
            else:
                scrubbed[key] = _scrub_payload(value, secrets)
        return scrubbed
    if isinstance(obj, list):
        return [_scrub_payload(item, secrets) for item in obj]
    if isinstance(obj, str):
        return _scrub_string(obj, secrets)
    return obj


def sentry_before_send(
    event: dict[str, Any],
    hint: dict[str, Any],
    *,
    allow_user_context: bool = False,
) -> dict[str, Any] | None:
    """丢弃控制流异常，并按黑名单净化错误事件。"""
    exc_info = hint.get("exc_info")
    if isinstance(exc_info, tuple) and len(exc_info) >= 2:
        error = exc_info[1]
        if isinstance(error, BaseException) and should_ignore_sentry_exception(error):
            return None

    if not allow_user_context:
        event.pop("user", None)
    return cast(
        "dict[str, Any]",
        _scrub_payload(event, _collect_sensitive_values()),
    )


def sentry_before_breadcrumb(
    breadcrumb: dict[str, Any],
    _hint: dict[str, Any],
) -> dict[str, Any]:
    """按黑名单净化 breadcrumb，诊断正文无条件保留。"""
    return cast(
        "dict[str, Any]",
        _scrub_payload(breadcrumb, _collect_sensitive_values()),
    )


def sentry_before_send_log(
    log: dict[str, Any],
    _hint: dict[str, Any],
) -> dict[str, Any]:
    """按黑名单净化 Sentry Logs，日志正文无条件保留。"""
    return cast(
        "dict[str, Any]",
        _scrub_payload(log, _collect_sensitive_values()),
    )


def sentry_before_send_transaction(
    event: dict[str, Any],
    _hint: dict[str, Any],
    *,
    allow_user_context: bool = False,
) -> dict[str, Any]:
    """按黑名单净化事务事件，阻止 tracing 绕过脱敏钩子。"""
    if not allow_user_context:
        event.pop("user", None)
    return cast(
        "dict[str, Any]",
        _scrub_payload(event, _collect_sensitive_values()),
    )


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
