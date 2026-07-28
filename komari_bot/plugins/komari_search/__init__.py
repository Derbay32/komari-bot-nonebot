"""Komari Search 插件，封装 Tavily 联网搜索能力。"""

import asyncio
import time
from typing import Any, Literal

from nonebot import get_driver, logger
from nonebot.plugin import PluginMetadata, require
from tavily import TavilyClient

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
_search_cache: dict[tuple[str, str, int, bool, int], tuple[float, str]] = {}


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
    """清洗并限制 Tavily 查询长度。"""
    return query.strip()[:_MAX_TAVILY_QUERY_CHARS]


def _build_cache_key(
    *,
    normalized_query: str,
    search_depth: str,
    max_results: int,
    include_answer: bool,
    result_content_limit: int,
) -> tuple[str, str, int, bool, int]:
    """构造不包含 API Key 的搜索缓存键。"""
    return (
        normalized_query,
        search_depth,
        max_results,
        include_answer,
        result_content_limit,
    )


def _get_cached_search_result(
    cache_key: tuple[str, str, int, bool, int],
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
    cache_key: tuple[str, str, int, bool, int],
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
        return "[搜索功能未启用]"
    if not api_key:
        return "[搜索失败：Tavily API Key 未配置]"
    if not normalized_query:
        return "[搜索失败：查询为空]"
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


async def search_web(query: str) -> str:
    """调用 Tavily API 搜索互联网并返回格式化结果文本。"""
    config = _get_config()
    api_key = config.tavily_api_key.strip()
    normalized_query = _normalize_query(query)
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
    now = time.monotonic()
    cached_result = _get_cached_search_result(cache_key, now=now)
    if cached_result is not None:
        return cached_result

    try:
        client = TavilyClient(api_key=api_key)
        async with _TAVILY_SEARCH_SEMAPHORE:
            response = await asyncio.to_thread(
                client.search,
                query=normalized_query,
                search_depth=search_depth,
                max_results=config.max_results,
                include_answer=config.include_answer,
            )
    except Exception as e:
        logger.warning("[KomariSearch] Tavily 搜索失败: query={} error={}", normalized_query, e)
        return f"[搜索失败：搜索服务异常 — {e}]"

    if not isinstance(response, dict):
        return "[搜索失败：搜索服务返回了无法识别的结果]"

    formatted_result = _format_search_response(
        response,
        include_answer=config.include_answer,
        result_content_limit=config.result_content_limit,
    )
    _store_cached_search_result(cache_key, formatted_result, now=time.monotonic())
    return formatted_result
