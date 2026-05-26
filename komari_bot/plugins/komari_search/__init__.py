"""Komari Search 插件，封装 Tavily 联网搜索能力。"""

import asyncio
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

    if not config.search_enabled:
        return "[搜索功能未启用]"

    api_key = config.tavily_api_key.strip()
    if not api_key:
        return "[搜索失败：Tavily API Key 未配置]"

    normalized_query = query.strip()
    if not normalized_query:
        return "[搜索失败：查询为空]"

    try:
        client = TavilyClient(api_key=api_key)
        search_depth: Literal["basic", "advanced"] = (
            "advanced" if config.search_depth == "advanced" else "basic"
        )
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

    return _format_search_response(
        response,
        include_answer=config.include_answer,
        result_content_limit=config.result_content_limit,
    )
