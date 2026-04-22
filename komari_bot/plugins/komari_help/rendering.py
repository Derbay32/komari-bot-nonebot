"""Komari Help 检索结果渲染。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .engine import get_config

if TYPE_CHECKING:
    from .models import HelpEntry, HelpSearchResult

LIST_PAGE_SIZE = 10


def category_emoji(category: str) -> str:
    mapping = {
        "command": "⌨️",
        "feature": "🧩",
        "faq": "❓",
        "other": "📄",
    }
    return mapping.get(category, "📄")


def get_search_result_limit() -> int:
    config = get_config()
    return min(config.default_result_limit, config.max_reply_result_count)


def format_content_lines(content: str) -> list[str]:
    lines = [line.rstrip() for line in content.strip().splitlines()]
    return lines or ["（无内容）"]


def format_results(results: list[HelpSearchResult]) -> str:
    config = get_config()
    display_results = results[: config.max_reply_result_count]
    lines = ["📖 帮助文档检索结果", "━━━━━━━━━━━━━━━"]
    for index, item in enumerate(display_results):
        if index > 0:
            lines.append("")
        emoji = (
            f"{category_emoji(item.category)} " if config.show_category_emoji else ""
        )
        lines.append(f"{emoji}{item.title}")
        lines.extend(f"  {line}" for line in format_content_lines(item.content))
    if len(results) > len(display_results):
        lines.extend(["", f"……其余 {len(results) - len(display_results)} 条结果已省略"])
    return "\n".join(lines)


def format_list_page(items: list[HelpEntry], total: int, page: int) -> str:
    total_pages = max((total + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE, 1)
    lines = [
        f"📚 当前帮助条目共 {total} 条（第 {page}/{total_pages} 页）",
        "━━━━━━━━━━━━━━━",
    ]
    config = get_config()
    for item in items:
        prefix = category_emoji(item.category) if config.show_category_emoji else "•"
        lines.append(f"{prefix} {item.title}")
    if page < total_pages:
        lines.append("")
        lines.append(f"查看下一页请使用 .docs list {page + 1}")
    return "\n".join(lines)
