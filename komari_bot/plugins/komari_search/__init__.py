"""Komari Search 插件，封装联网搜索与网页抓取能力（Tavily / EXA 双提供者）。"""

import asyncio
import hashlib
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from urllib.parse import urlparse

from nonebot import get_driver, logger
from nonebot.plugin import PluginMetadata, require

from komari_bot.common.content_budget import (
    QUERY_TEXT_BUDGET,
    ContentValidationError,
    normalize_required_text,
)

from .api import register_search_api
from .config import Config
from .config_schema import DynamicConfigSchema
from .formatter import format_fetch_response, format_search_response
from .provider import get_provider
from .types import FetchResponse, SearchResponse

__plugin_meta__ = PluginMetadata(
    name="komari_search",
    description="小鞠联网搜索与网页抓取服务",
    usage=(
        "komari_search = require('komari_search'); "
        "await komari_search.search_web('关键词'); "
        "await komari_search.fetch_page(['https://example.com'])"
    ),
    config=Config,
)

__all__ = [
    "config_manager",
    "fetch_page",
    "is_fetch_available",
    "is_search_available",
    "register_search_api",
    "search_web",
    "shutdown_search_resources",
]

config_manager_plugin = require("config_manager")
permission_manager_plugin = require("permission_manager")
config_manager = config_manager_plugin.get_config_manager(
    "komari_search",
    DynamicConfigSchema,
)

_MAX_SEARCH_QUERY_CHARS = 200
_SEARCH_CONCURRENCY_LIMIT = 2
_FETCH_CONCURRENCY_LIMIT = 2
_SEARCH_SEMAPHORE = asyncio.Semaphore(_SEARCH_CONCURRENCY_LIMIT)
_FETCH_SEMAPHORE = asyncio.Semaphore(_FETCH_CONCURRENCY_LIMIT)
_SEARCH_CACHE_TTL_SECONDS = 60.0
_SEARCH_CACHE_MAX_SIZE = 128
# 与 komari_chat llm_service 的 _MAX_TOOL_RESULT_CHARS 保持一致；
# 若该常量调整，fetch 总量兜底联动变化。
_FETCH_TOTAL_CONTENT_LIMIT = 8_000

# 缓存键：(provider, query, max_results, result_content_limit,
#          tavily_search_depth, tavily_include_answer, exa_search_type)
type SearchCacheKey = tuple[str, str, int, int, str, bool, str]
_search_cache: dict[SearchCacheKey, tuple[float, str]] = {}
_search_inflight: dict[SearchCacheKey, asyncio.Task[str]] = {}

# fetch single-flight 键为 URL 集合，不缓存结果
type FetchFlightKey = frozenset[str]
_fetch_inflight: dict[FetchFlightKey, asyncio.Task[str]] = {}

_SEARCH_ERROR_DISABLED = "[搜索失败：DISABLED]"
_SEARCH_ERROR_PERMISSION = "[搜索失败：PERMISSION_DENIED]"
_SEARCH_ERROR_CONFIG = "[搜索失败：CONFIG_ERROR]"
_SEARCH_ERROR_INVALID_QUERY = "[搜索失败：INVALID_QUERY]"
_SEARCH_ERROR_TIMEOUT = "[搜索失败：TIMEOUT]"
_SEARCH_ERROR_CIRCUIT_OPEN = "[搜索失败：CIRCUIT_OPEN]"
_SEARCH_ERROR_UPSTREAM = "[搜索失败：UPSTREAM_ERROR]"
_SEARCH_QUERY_LIMIT_ERROR = (
    f"联网搜索查询不能超过 {_MAX_SEARCH_QUERY_CHARS} 个字符"
)

_FETCH_ERROR_DISABLED = "[抓取失败：DISABLED]"
_FETCH_ERROR_PERMISSION = "[抓取失败：PERMISSION_DENIED]"
_FETCH_ERROR_CONFIG = "[抓取失败：CONFIG_ERROR]"
_FETCH_ERROR_INVALID_URLS = "[抓取失败：INVALID_URLS]"
_FETCH_ERROR_TIMEOUT = "[抓取失败：TIMEOUT]"
_FETCH_ERROR_CIRCUIT_OPEN = "[抓取失败：CIRCUIT_OPEN]"
_FETCH_ERROR_UPSTREAM = "[抓取失败：UPSTREAM_ERROR]"

_FETCH_URL_NOT_STRING_ERROR = "抓取 URL 必须是字符串"
_FETCH_URL_EMPTY_ERROR = "抓取 URL 不能为空"
_FETCH_URL_INVALID_ERROR = "抓取 URL 必须是合法的 http/https 地址"
_FETCH_URL_LIST_EMPTY_ERROR = "抓取 URL 列表不能为空"


class _ExecutorState:
    """按需创建有并发上限的提供者调用线程池。"""

    def __init__(self) -> None:
        self.executor: ThreadPoolExecutor | None = None

    def get(self) -> ThreadPoolExecutor:
        if self.executor is None:
            self.executor = ThreadPoolExecutor(
                max_workers=_SEARCH_CONCURRENCY_LIMIT + _FETCH_CONCURRENCY_LIMIT,
                thread_name_prefix="komari-search",
            )
        return self.executor

    def close(self) -> None:
        executor = self.executor
        self.executor = None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)


class _CircuitBreaker:
    """进程内连续失败熔断器，仅允许一个半开探测。"""

    def __init__(self) -> None:
        self.consecutive_failures = 0
        self.open_until = 0.0
        self.half_open_probe_active = False

    def begin_request(self, *, now: float) -> bool:
        if self.open_until > now:
            return False
        if self.open_until > 0:
            if self.half_open_probe_active:
                return False
            self.half_open_probe_active = True
        return True

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.open_until = 0.0
        self.half_open_probe_active = False

    def record_failure(
        self,
        *,
        now: float,
        threshold: int,
        recovery_seconds: float,
    ) -> None:
        self.consecutive_failures += 1
        self.half_open_probe_active = False
        if self.consecutive_failures >= threshold:
            self.open_until = now + recovery_seconds

    def cancel_request(self) -> None:
        self.half_open_probe_active = False

    def reset(self) -> None:
        self.consecutive_failures = 0
        self.open_until = 0.0
        self.half_open_probe_active = False


_executor_state = _ExecutorState()
_search_circuit_breaker = _CircuitBreaker()
_fetch_circuit_breaker = _CircuitBreaker()


def _get_config() -> DynamicConfigSchema:
    """读取动态配置，并用 .env 中的搜索 Key 作为未持久化时的兜底。"""
    config = config_manager.get()
    if config.search_api_key.strip():
        return config

    try:
        driver_config = get_driver().config
    except ValueError:
        return config

    env_key = str(getattr(driver_config, "search_api_key", "")).strip()
    if not env_key:
        return config

    return config.model_copy(update={"search_api_key": env_key})


def _is_caller_allowed(
    config: DynamicConfigSchema,
    *,
    caller_user_id: str | None,
    caller_group_id: str | None,
    caller_is_superuser: bool,
) -> bool:
    """对调用者执行统一动态权限检查；缺失受限上下文时默认拒绝。"""
    if not caller_is_superuser:
        if config.user_whitelist and not caller_user_id:
            return False
        if config.group_whitelist and not caller_group_id:
            return False
    allowed, _reason = permission_manager_plugin.check_context_permission(
        config,
        user_id=caller_user_id or "",
        group_id=caller_group_id,
        is_superuser=caller_is_superuser,
    )
    return bool(allowed)


def is_search_available(
    *,
    caller_user_id: str | None = None,
    caller_group_id: str | None = None,
    caller_is_superuser: bool = False,
) -> bool:
    """判断当前调用者是否具备注册 search_web 工具的条件。"""
    config = _get_config()
    return (
        config.search_enabled
        and bool(config.search_api_key.strip())
        and _is_caller_allowed(
            config,
            caller_user_id=caller_user_id,
            caller_group_id=caller_group_id,
            caller_is_superuser=caller_is_superuser,
        )
    )


def is_fetch_available(
    *,
    caller_user_id: str | None = None,
    caller_group_id: str | None = None,
    caller_is_superuser: bool = False,
) -> bool:
    """判断当前调用者是否具备注册 fetch_page 工具的条件。"""
    config = _get_config()
    return (
        config.fetch_enabled
        and bool(config.search_api_key.strip())
        and _is_caller_allowed(
            config,
            caller_user_id=caller_user_id,
            caller_group_id=caller_group_id,
            caller_is_superuser=caller_is_superuser,
        )
    )


def _normalize_query(query: str) -> str:
    """按共享预算清洗查询，并拒绝提供者无法接受的超长输入。"""
    normalized = normalize_required_text(
        query,
        label="联网搜索查询",
        budget=QUERY_TEXT_BUDGET,
    )
    if len(normalized) > _MAX_SEARCH_QUERY_CHARS:
        raise ContentValidationError(_SEARCH_QUERY_LIMIT_ERROR)
    return normalized


def _normalize_urls(urls: list[str]) -> list[str]:
    """清洗并去重 URL 列表；非法 URL 抛出 ContentValidationError。"""
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_url in urls:
        if not isinstance(raw_url, str):
            raise ContentValidationError(_FETCH_URL_NOT_STRING_ERROR)
        candidate = raw_url.strip()
        if not candidate:
            raise ContentValidationError(_FETCH_URL_EMPTY_ERROR)
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ContentValidationError(_FETCH_URL_INVALID_ERROR)
        if candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)
    if not normalized:
        raise ContentValidationError(_FETCH_URL_LIST_EMPTY_ERROR)
    return normalized


def _build_cache_key(
    *,
    config: DynamicConfigSchema,
    normalized_query: str,
) -> SearchCacheKey:
    """构造不包含 API Key 的搜索缓存键（含 provider）。"""
    return (
        config.search_provider,
        normalized_query,
        config.max_results,
        config.result_content_limit,
        config.tavily_search_depth,
        config.tavily_include_answer,
        config.exa_search_type,
    )


def _get_cached_search_result(
    cache_key: SearchCacheKey,
    *,
    now: float,
) -> str | None:
    """读取未过期的搜索缓存。"""
    cached = _search_cache.get(cache_key)
    if cached is None:
        return None
    expires_at, result = cached
    if expires_at <= now:
        _search_cache.pop(cache_key, None)
        return None
    return result


def _store_cached_search_result(
    cache_key: SearchCacheKey,
    result: str,
    *,
    now: float,
) -> None:
    """写入搜索缓存并按 TTL/容量清理。"""
    _search_cache[cache_key] = (now + _SEARCH_CACHE_TTL_SECONDS, result)
    expired_keys = [
        key for key, (expires_at, _result) in _search_cache.items() if expires_at <= now
    ]
    for key in expired_keys:
        _search_cache.pop(key, None)
    while len(_search_cache) > _SEARCH_CACHE_MAX_SIZE:
        oldest_key = next(iter(_search_cache))
        _search_cache.pop(oldest_key, None)


def _get_search_precheck_error(
    *,
    config: DynamicConfigSchema,
    api_key: str,
    normalized_query: str,
    caller_user_id: str | None,
    caller_group_id: str | None,
    caller_is_superuser: bool,
) -> str | None:
    """返回搜索调用前的配置/查询错误。"""
    if not config.plugin_enable:
        return _SEARCH_ERROR_DISABLED
    if not config.search_enabled:
        return _SEARCH_ERROR_DISABLED
    if not _is_caller_allowed(
        config,
        caller_user_id=caller_user_id,
        caller_group_id=caller_group_id,
        caller_is_superuser=caller_is_superuser,
    ):
        return _SEARCH_ERROR_PERMISSION
    if not api_key:
        return _SEARCH_ERROR_CONFIG
    if not normalized_query:
        return _SEARCH_ERROR_INVALID_QUERY
    return None


def _get_fetch_precheck_error(
    *,
    config: DynamicConfigSchema,
    api_key: str,
    caller_user_id: str | None,
    caller_group_id: str | None,
    caller_is_superuser: bool,
) -> str | None:
    """返回抓取调用前的配置错误。"""
    if not config.plugin_enable:
        return _FETCH_ERROR_DISABLED
    if not config.fetch_enabled:
        return _FETCH_ERROR_DISABLED
    if not _is_caller_allowed(
        config,
        caller_user_id=caller_user_id,
        caller_group_id=caller_group_id,
        caller_is_superuser=caller_is_superuser,
    ):
        return _FETCH_ERROR_PERMISSION
    if not api_key:
        return _FETCH_ERROR_CONFIG
    return None


def _safe_trace_id(value: str | None) -> str:
    raw_value = str(value or "").strip()[:128]
    return "".join(
        char
        for char in raw_value
        if char.isascii() and (char.isalnum() or char in "-_.:")
    )


def _log_search_failure(
    *,
    query: str,
    request_trace_id: str | None,
    code: str,
    error_type: str,
) -> None:
    """只记录查询指纹与稳定错误分类。"""
    logger.warning(
        "[KomariSearch] 搜索失败: trace_id={} query_sha256={} query_chars={} code={} error_type={}",
        _safe_trace_id(request_trace_id) or "-",
        hashlib.sha256(query.encode()).hexdigest(),
        len(query),
        code,
        error_type,
    )


def _log_fetch_failure(
    *,
    urls: list[str],
    request_trace_id: str | None,
    code: str,
    error_type: str,
) -> None:
    """只记录 URL 数量、URL 集合指纹与稳定错误分类，不记录 URL 内容。"""
    logger.warning(
        "[KomariSearch] 抓取失败: trace_id={} url_count={} urls_sha256={} code={} error_type={}",
        _safe_trace_id(request_trace_id) or "-",
        len(urls),
        hashlib.sha256("\n".join(urls).encode()).hexdigest(),
        code,
        error_type,
    )


async def _run_provider_request(request: Callable[[], object]) -> object:
    """在专用线程池中执行同步 SDK 调用。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor_state.get(), request)


def _record_search_failure(config: DynamicConfigSchema) -> None:
    _search_circuit_breaker.record_failure(
        now=time.monotonic(),
        threshold=config.circuit_breaker_failure_threshold,
        recovery_seconds=config.circuit_breaker_recovery_seconds,
    )


def _record_fetch_failure(config: DynamicConfigSchema) -> None:
    _fetch_circuit_breaker.record_failure(
        now=time.monotonic(),
        threshold=config.circuit_breaker_failure_threshold,
        recovery_seconds=config.circuit_breaker_recovery_seconds,
    )


async def _execute_search(
    *,
    config: DynamicConfigSchema,
    api_key: str,
    normalized_query: str,
    cache_key: SearchCacheKey,
    request_trace_id: str | None,
) -> str:
    """执行一次可共享的上游搜索请求，并维护熔断与成功缓存。"""
    if not _search_circuit_breaker.begin_request(now=time.monotonic()):
        _log_search_failure(
            query=normalized_query,
            request_trace_id=request_trace_id,
            code="CIRCUIT_OPEN",
            error_type="CircuitOpen",
        )
        return _SEARCH_ERROR_CIRCUIT_OPEN

    try:
        async with asyncio.timeout(config.search_timeout_seconds):
            async with _SEARCH_SEMAPHORE:
                provider = get_provider(config.search_provider)
                response = await _run_provider_request(
                    partial(
                        provider.search,
                        api_key=api_key,
                        query=normalized_query,
                        config=config,
                    )
                )
    except TimeoutError:
        _record_search_failure(config)
        _log_search_failure(
            query=normalized_query,
            request_trace_id=request_trace_id,
            code="TIMEOUT",
            error_type="TimeoutError",
        )
        return _SEARCH_ERROR_TIMEOUT
    except asyncio.CancelledError:
        _search_circuit_breaker.cancel_request()
        raise
    except Exception as exc:
        _record_search_failure(config)
        _log_search_failure(
            query=normalized_query,
            request_trace_id=request_trace_id,
            code="UPSTREAM_ERROR",
            error_type=type(exc).__name__,
        )
        return _SEARCH_ERROR_UPSTREAM

    if not isinstance(response, SearchResponse):
        _record_search_failure(config)
        _log_search_failure(
            query=normalized_query,
            request_trace_id=request_trace_id,
            code="INVALID_RESPONSE",
            error_type=type(response).__name__,
        )
        return _SEARCH_ERROR_UPSTREAM

    _search_circuit_breaker.record_success()
    formatted_result = format_search_response(
        response,
        result_content_limit=config.result_content_limit,
    )
    _store_cached_search_result(cache_key, formatted_result, now=time.monotonic())
    return formatted_result


def _get_or_create_search_task(
    *,
    config: DynamicConfigSchema,
    api_key: str,
    normalized_query: str,
    cache_key: SearchCacheKey,
    request_trace_id: str | None,
) -> asyncio.Task[str]:
    existing = _search_inflight.get(cache_key)
    if existing is not None:
        return existing

    task = asyncio.create_task(
        _execute_search(
            config=config,
            api_key=api_key,
            normalized_query=normalized_query,
            cache_key=cache_key,
            request_trace_id=request_trace_id,
        ),
        name="komari-search-singleflight",
    )
    _search_inflight[cache_key] = task

    def _remove_completed(completed: asyncio.Task[str]) -> None:
        if _search_inflight.get(cache_key) is completed:
            _search_inflight.pop(cache_key, None)

    task.add_done_callback(_remove_completed)
    return task


async def search_web(
    query: str,
    *,
    request_trace_id: str | None = None,
    caller_user_id: str | None = None,
    caller_group_id: str | None = None,
    caller_is_superuser: bool = False,
) -> str:
    """调用配置的搜索提供者搜索互联网并返回格式化结果文本。"""
    config = _get_config()
    api_key = config.search_api_key.strip()
    if not isinstance(query, str):
        return _SEARCH_ERROR_INVALID_QUERY
    try:
        normalized_query = _normalize_query(query)
    except ContentValidationError:
        return _SEARCH_ERROR_INVALID_QUERY

    precheck_error = _get_search_precheck_error(
        config=config,
        api_key=api_key,
        normalized_query=normalized_query,
        caller_user_id=caller_user_id,
        caller_group_id=caller_group_id,
        caller_is_superuser=caller_is_superuser,
    )
    if precheck_error is not None:
        return precheck_error

    cache_key = _build_cache_key(config=config, normalized_query=normalized_query)
    cached_result = _get_cached_search_result(cache_key, now=time.monotonic())
    if cached_result is not None:
        return cached_result

    task = _get_or_create_search_task(
        config=config,
        api_key=api_key,
        normalized_query=normalized_query,
        cache_key=cache_key,
        request_trace_id=request_trace_id,
    )
    return await asyncio.shield(task)


async def _execute_fetch(
    *,
    config: DynamicConfigSchema,
    api_key: str,
    urls: list[str],
    request_trace_id: str | None,
) -> str:
    """执行一次可共享的上游抓取请求，并维护独立熔断。"""
    if not _fetch_circuit_breaker.begin_request(now=time.monotonic()):
        _log_fetch_failure(
            urls=urls,
            request_trace_id=request_trace_id,
            code="CIRCUIT_OPEN",
            error_type="CircuitOpen",
        )
        return _FETCH_ERROR_CIRCUIT_OPEN

    try:
        async with asyncio.timeout(config.fetch_timeout_seconds):
            async with _FETCH_SEMAPHORE:
                provider = get_provider(config.search_provider)
                response = await _run_provider_request(
                    partial(provider.fetch, api_key=api_key, urls=urls, config=config)
                )
    except TimeoutError:
        _record_fetch_failure(config)
        _log_fetch_failure(
            urls=urls,
            request_trace_id=request_trace_id,
            code="TIMEOUT",
            error_type="TimeoutError",
        )
        return _FETCH_ERROR_TIMEOUT
    except asyncio.CancelledError:
        _fetch_circuit_breaker.cancel_request()
        raise
    except Exception as exc:
        _record_fetch_failure(config)
        _log_fetch_failure(
            urls=urls,
            request_trace_id=request_trace_id,
            code="UPSTREAM_ERROR",
            error_type=type(exc).__name__,
        )
        return _FETCH_ERROR_UPSTREAM

    if not isinstance(response, FetchResponse):
        _record_fetch_failure(config)
        _log_fetch_failure(
            urls=urls,
            request_trace_id=request_trace_id,
            code="INVALID_RESPONSE",
            error_type=type(response).__name__,
        )
        return _FETCH_ERROR_UPSTREAM

    _fetch_circuit_breaker.record_success()
    return format_fetch_response(
        response,
        content_limit=config.fetch_content_limit,
        total_limit=_FETCH_TOTAL_CONTENT_LIMIT,
    )


def _get_or_create_fetch_task(
    *,
    config: DynamicConfigSchema,
    api_key: str,
    urls: list[str],
    flight_key: FetchFlightKey,
    request_trace_id: str | None,
) -> asyncio.Task[str]:
    existing = _fetch_inflight.get(flight_key)
    if existing is not None:
        return existing

    task = asyncio.create_task(
        _execute_fetch(
            config=config,
            api_key=api_key,
            urls=urls,
            request_trace_id=request_trace_id,
        ),
        name="komari-fetch-singleflight",
    )
    _fetch_inflight[flight_key] = task

    def _remove_completed(completed: asyncio.Task[str]) -> None:
        if _fetch_inflight.get(flight_key) is completed:
            _fetch_inflight.pop(flight_key, None)

    task.add_done_callback(_remove_completed)
    return task


async def fetch_page(
    urls: list[str],
    *,
    request_trace_id: str | None = None,
    caller_user_id: str | None = None,
    caller_group_id: str | None = None,
    caller_is_superuser: bool = False,
) -> str:
    """抓取指定网页正文并返回格式化结果文本（仅 single-flight 去重，不缓存）。"""
    config = _get_config()
    api_key = config.search_api_key.strip()

    precheck_error = _get_fetch_precheck_error(
        config=config,
        api_key=api_key,
        caller_user_id=caller_user_id,
        caller_group_id=caller_group_id,
        caller_is_superuser=caller_is_superuser,
    )
    if precheck_error is not None:
        return precheck_error

    if not isinstance(urls, list) or not urls:
        return _FETCH_ERROR_INVALID_URLS
    try:
        normalized_urls = _normalize_urls(urls)
    except ContentValidationError:
        return _FETCH_ERROR_INVALID_URLS
    if len(normalized_urls) > config.fetch_max_urls:
        return _FETCH_ERROR_INVALID_URLS

    flight_key: FetchFlightKey = frozenset(normalized_urls)
    task = _get_or_create_fetch_task(
        config=config,
        api_key=api_key,
        urls=normalized_urls,
        flight_key=flight_key,
        request_trace_id=request_trace_id,
    )
    return await asyncio.shield(task)


async def shutdown_search_resources() -> None:
    """取消未完成的 search/fetch single-flight，并停止专用线程池接收新请求。"""
    tasks = [*list(_search_inflight.values()), *list(_fetch_inflight.values())]
    _search_inflight.clear()
    _fetch_inflight.clear()
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _executor_state.close()


def _reset_search_runtime_for_tests() -> None:
    """清空进程内短期状态，避免测试之间相互影响。"""
    _search_cache.clear()
    _search_inflight.clear()
    _fetch_inflight.clear()
    _search_circuit_breaker.reset()
    _fetch_circuit_breaker.reset()


try:
    driver = get_driver()
except ValueError:
    driver = None

if driver is not None:

    @driver.on_shutdown
    async def _shutdown() -> None:
        await shutdown_search_resources()
