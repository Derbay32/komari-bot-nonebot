"""Komari Search 统一格式化：归一化结构 → LLM 工具结果文本。"""

from .types import FetchResponse, SearchResponse


def format_search_response(
    response: SearchResponse,
    *,
    result_content_limit: int,
) -> str:
    """将归一化搜索响应格式化为适合 LLM 消费的工具结果文本。"""
    parts: list[str] = []

    if response.answer:
        parts.append(f"搜索摘要：{response.answer}")

    if response.items:
        parts.append("搜索结果：")
        for index, item in enumerate(response.items, start=1):
            content = item.content
            if len(content) > result_content_limit:
                content = f"{content[:result_content_limit].rstrip()}…"
            parts.append(f"{index}. {item.title}\n链接：{item.url}\n摘要：{content}")

    if parts:
        return "\n\n".join(parts)
    return "[搜索完成，但未找到相关结果]"


def format_fetch_response(
    response: FetchResponse,
    *,
    content_limit: int,
    total_limit: int,
) -> str:
    """将归一化抓取响应格式化工具结果文本。

    每条结果按 ``content_limit`` 截断；全部结果拼接后若超 ``total_limit``，
    按页面顺序截断到上限。
    """
    if not response.items:
        return "[抓取完成，但未获取到页面内容]"

    parts: list[str] = []
    for index, item in enumerate(response.items, start=1):
        content = item.content
        if len(content) > content_limit:
            content = f"{content[:content_limit].rstrip()}…"
        title = item.title or "无标题"
        parts.append(f"[页面 {index}] {title}\n链接：{item.url}\n正文：{content}")

    joined = "\n\n".join(parts)
    if len(joined) > total_limit:
        joined = f"{joined[:total_limit].rstrip()}…"
    return joined
