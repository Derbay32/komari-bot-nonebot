"""Komari Help 检索结果渲染。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .engine import get_config

if TYPE_CHECKING:
    from .models import HelpSearchResult


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
        lines.append(
            f"{emoji}{item.title}"
            + (f" ({item.plugin_name})" if item.plugin_name else "")
        )
        lines.extend(f"  {line}" for line in format_content_lines(item.content))
    if len(results) > len(display_results):
        lines.extend(["", f"……其余 {len(results) - len(display_results)} 条结果已省略"])
    return "\n".join(lines)
