"""Komari Search 插件，封装 Tavily 联网搜索能力。"""

import asyncio
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Literal

from nonebot import get_driver, logger
from nonebot.plugin import PluginMetadata, require
from tavily import TavilyClient

from komari_bot.common.content_budget import (
    QUERY_TEXT_BUDGET,
    ContentValidationError,
    normalize_required_text,
)

from .config import Config
from .config_schema import DynamicConfigSchema

__plugin_meta__ = PluginMetadata(
    name="komari_search",
    description="小鞠联网搜索服务，封装 Tavily Search API 供其他插件复用",
    usage="komari_search = require('komari_search'); await komari_search.search_web('关键词')",
    config=Config,
)

__all__ = [
    "config_manager",
    "is_search_available",
    "search_web",
    "shutdown_search_resources",
]

config_manager_plugin = require("config_manager")
config_manager = config_manager_plugin.get_config_manager(
    "komari_search",
    DynamicConfigSchema,
)

_MAX_TAVILY_QUERY_CHARS = 200
_TAVILY_SEARCH_CONCURRENCY_LIMIT = 2
_TAVILY_SEARCH_SEMAPHORE = asyncio.Semaphore(_TAVILY_SEARCH_CONCURRENCY_LIMIT)
_SEARCH_CACHE_TTL_SECONDS = 60.0
_SEARCH_CACHE_MAX_SIZE = 128
type SearchCacheKey = tuple[str, str, int, bool, int]
_search_cache: dict[SearchCacheKey, tuple[float, str]] = {}
_search_inflight: dict[SearchCacheKey, asyncio.Task[str]] = {}

_SEARCH_ERROR_DISABLED = "[搜索失败：DISABLED]"
_SEARCH_ERROR_CONFIG = "[搜索失败：CONFIG_ERROR]"
_SEARCH_ERROR_INVALID_QUERY = "[搜索失败：INVALID_QUERY]"
_SEARCH_ERROR_TIMEOUT = "[搜索失败：TIMEOUT]"
_SEARCH_ERROR_CIRCUIT_OPEN = "[搜索失败：CIRCUIT_OPEN]"
_SEARCH_ERROR_UPSTREAM = "[搜索失败：UPSTREAM_ERROR]"
_SEARCH_ERROR_INVALID_RESPONSE = "[搜索失败：INVALID_RESPONSE]"
_TAVILY_QUERY_LIMIT_ERROR = (
    f"联网搜索查询不能超过 {_MAX_TAVILY_QUERY_CHARS} 个字符"
)


class _SearchExecutorState:
    """按需创建有并发上限的搜索线程池。"""

    def __init__(self) -> None:
        self.executor: ThreadPoolExecutor | None = None

    def get(self) -> ThreadPoolExecutor:
        if self.executor is None:
            self.executor = ThreadPoolExecutor(
                max_workers=_TAVILY_SEARCH_CONCURRENCY_LIMIT,
                thread_name_prefix="komari-search",
            )
        return self.executor

    def close(self) -> None:
        executor = self.executor
        self.executor = None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)


class _SearchCircuitBreaker:
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


_executor_state = _SearchExecutorState()
_circuit_breaker = _SearchCircuitBreaker()


def _get_config() -> DynamicConfigSchema:
    """读取动态配置，并用 .env 中的 Tavily Key 作为未持久化时的兜底。"""
    config = config_manager.get()
    if config.tavily_api_key.strip():
        return config

    try:
        driver_config = get_driver().config
    except ValueError:
        return config

    env_key = str(getattr(driver_config, "tavily_api_key", "")).strip()
    if not env_key:
        return config

    return config.model_copy(update={"tavily_api_key": env_key})


def is_search_available() -> bool:
    """判断是否具备注册 search_web 工具的条件。"""
    config = _get_config()
    return config.search_enabled and bool(config.tavily_api_key.strip())


def _normalize_query(query: str) -> str:
    """按共享预算清洗查询，并拒绝 Tavily 无法接受的超长输入。"""
    normalized = normalize_required_text(
        query,
        label="联网搜索查询",
        budget=QUERY_TEXT_BUDGET,
    )
    if len(normalized) > _MAX_TAVILY_QUERY_CHARS:
        raise ContentValidationError(_TAVILY_QUERY_LIMIT_ERROR)
    return normalized


def _build_cache_key(
    *,
    normalized_query: str,
    search_depth: str,
    max_results: int,
    include_answer: bool,
    result_content_limit: int,
) -> SearchCacheKey:
    """构造不包含 API Key 的搜索缓存键。"""
    return (
        normalized_query,
        search_depth,
        max_results,
        include_answer,
        result_content_limit,
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
) -> str | None:
    """返回搜索调用前的配置/查询错误。"""
    if not config.search_enabled:
        return _SEARCH_ERROR_DISABLED
    if not api_key:
        return _SEARCH_ERROR_CONFIG
    if not normalized_query:
        return _SEARCH_ERROR_INVALID_QUERY
    return None


def _format_search_response(
    response: dict[str, Any],
    *,
    include_answer: bool,
    result_content_limit: int,
) -> str:
    """将 Tavily 响应格式化为适合 LLM 消费的工具结果文本。"""
    parts: list[str] = []

    answer = response.get("answer")
    if include_answer and isinstance(answer, str) and answer.strip():
        parts.append(f"搜索摘要：{answer.strip()}")

    raw_results = response.get("results")
    results = raw_results if isinstance(raw_results, list) else []
    if results:
        parts.append("搜索结果：")
        for index, raw_result in enumerate(results, start=1):
            if not isinstance(raw_result, dict):
                continue
            title = str(raw_result.get("title") or "无标题").strip()
            url = str(raw_result.get("url") or "").strip()
            content = str(raw_result.get("content") or "").strip()
            if len(content) > result_content_limit:
                content = f"{content[:result_content_limit].rstrip()}…"
            parts.append(f"{index}. {title}\n链接：{url}\n摘要：{content}")

    if parts:
        return "\n\n".join(parts)
    return "[搜索完成，但未找到相关结果]"


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


def _search_tavily_sync(
    *,
    api_key: str,
    query: str,
    search_depth: Literal["basic", "advanced"],
    max_results: int,
    include_answer: bool,
    timeout_seconds: float,
) -> object:
    """在线程池中执行同步 Tavily SDK，并下发传输层超时。"""
    client = TavilyClient(api_key=api_key)
    try:
        return client.search(
            query=query,
            search_depth=search_depth,
            max_results=max_results,
            include_answer=include_answer,
            timeout=timeout_seconds,
        )
    finally:
        client.close()


async def _run_tavily_request(
    *,
    api_key: str,
    query: str,
    search_depth: Literal["basic", "advanced"],
    max_results: int,
    include_answer: bool,
    timeout_seconds: float,
) -> object:
    loop = asyncio.get_running_loop()
    request = partial(
        _search_tavily_sync,
        api_key=api_key,
        query=query,
        search_depth=search_depth,
        max_results=max_results,
        include_answer=include_answer,
        timeout_seconds=timeout_seconds,
    )
    return await loop.run_in_executor(_executor_state.get(), request)


def _record_search_failure(config: DynamicConfigSchema) -> None:
    _circuit_breaker.record_failure(
        now=time.monotonic(),
        threshold=config.circuit_breaker_failure_threshold,
        recovery_seconds=config.circuit_breaker_recovery_seconds,
    )


async def _execute_search(
    *,
    config: DynamicConfigSchema,
    api_key: str,
    normalized_query: str,
    search_depth: Literal["basic", "advanced"],
    cache_key: SearchCacheKey,
    request_trace_id: str | None,
) -> str:
    """执行一次可共享的上游请求，并维护熔断与成功缓存。"""
    if not _circuit_breaker.begin_request(now=time.monotonic()):
        _log_search_failure(
            query=normalized_query,
            request_trace_id=request_trace_id,
            code="CIRCUIT_OPEN",
            error_type="CircuitOpen",
        )
        return _SEARCH_ERROR_CIRCUIT_OPEN

    try:
        async with asyncio.timeout(config.search_timeout_seconds):
            async with _TAVILY_SEARCH_SEMAPHORE:
                response = await _run_tavily_request(
                    api_key=api_key,
                    query=normalized_query,
                    search_depth=search_depth,
                    max_results=config.max_results,
                    include_answer=config.include_answer,
                    timeout_seconds=config.search_timeout_seconds,
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
        _circuit_breaker.cancel_request()
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

    if not isinstance(response, dict):
        _record_search_failure(config)
        _log_search_failure(
            query=normalized_query,
            request_trace_id=request_trace_id,
            code="INVALID_RESPONSE",
            error_type=type(response).__name__,
        )
        return _SEARCH_ERROR_INVALID_RESPONSE

    _circuit_breaker.record_success()
    formatted_result = _format_search_response(
        response,
        include_answer=config.include_answer,
        result_content_limit=config.result_content_limit,
    )
    _store_cached_search_result(cache_key, formatted_result, now=time.monotonic())
    return formatted_result


def _get_or_create_search_task(
    *,
    config: DynamicConfigSchema,
    api_key: str,
    normalized_query: str,
    search_depth: Literal["basic", "advanced"],
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
            search_depth=search_depth,
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
) -> str:
    """调用 Tavily API 搜索互联网并返回格式化结果文本。"""
    config = _get_config()
    api_key = config.tavily_api_key.strip()
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
    )
    if precheck_error is not None:
        return precheck_error

    search_depth: Literal["basic", "advanced"] = (
        "advanced" if config.search_depth == "advanced" else "basic"
    )
    cache_key = _build_cache_key(
        normalized_query=normalized_query,
        search_depth=search_depth,
        max_results=config.max_results,
        include_answer=config.include_answer,
        result_content_limit=config.result_content_limit,
    )
    cached_result = _get_cached_search_result(cache_key, now=time.monotonic())
    if cached_result is not None:
        return cached_result

    task = _get_or_create_search_task(
        config=config,
        api_key=api_key,
        normalized_query=normalized_query,
        search_depth=search_depth,
        cache_key=cache_key,
        request_trace_id=request_trace_id,
    )
    return await asyncio.shield(task)


async def shutdown_search_resources() -> None:
    """取消未完成的 single-flight，并停止专用线程池接收新请求。"""
    tasks = list(_search_inflight.values())
    _search_inflight.clear()
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _executor_state.close()


def _reset_search_runtime_for_tests() -> None:
    """清空进程内短期状态，避免测试之间相互影响。"""
    _search_cache.clear()
    _search_inflight.clear()
    _circuit_breaker.reset()


try:
    driver = get_driver()
except ValueError:
    driver = None

if driver is not None:

    @driver.on_shutdown
    async def _shutdown() -> None:
        await shutdown_search_resources()
