"""Komari Help 命令处理器。"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from nonebot import logger, on_command
from nonebot.adapters.onebot.v11 import Message  # noqa: TC002
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER

from .engine import get_config, get_engine
from .scanner import scan_and_sync

if TYPE_CHECKING:
    from .models import HelpSearchResult

help_cmd = on_command("help", aliases={"帮助"}, priority=10, block=True)
help_list_cmd = on_command(("help", "list"), priority=9, block=True)
help_refresh_cmd = on_command(
    ("help", "refresh"),
    permission=SUPERUSER,
    priority=5,
    block=True,
)


def _category_emoji(category: str) -> str:
    mapping = {
        "command": "⌨️",
        "feature": "🧩",
        "faq": "❓",
        "other": "📄",
    }
    return mapping.get(category, "📄")


def _preview_content(content: str) -> str:
    max_length = get_config().max_content_preview_length
    normalized = " ".join(content.split())
    if len(normalized) <= max_length:
        return normalized
    return f"{normalized[:max_length].rstrip()}…"


def _format_results(results: list[HelpSearchResult]) -> str:
    config = get_config()
    lines = ["📖 帮助文档检索结果", "━━━━━━━━━━━━━━━"]
    for item in results:
        category = item.category
        emoji = f"{_category_emoji(category)} " if config.show_category_emoji else ""
        title = item.title
        plugin_name = item.plugin_name
        content = item.content
        lines.append(f"{emoji}{title}" + (f" ({plugin_name})" if plugin_name else ""))
        lines.append(f"  {_preview_content(content)}")
    return "\n".join(lines)


async def _build_overview() -> str:
    engine = get_engine()
    if engine is None:
        return "帮助引擎尚未初始化。"
    items, _ = await engine.list_help(limit=200, offset=0)
    if not items:
        return "当前还没有可用的帮助条目。"

    grouped: dict[str, list[str]] = defaultdict(list)
    for item in items:
        key = item.plugin_name or "未分类插件"
        grouped[key].append(item.title)

    lines = ["📚 帮助概览", "━━━━━━━━━━━━━━━"]
    for plugin_name in sorted(grouped):
        lines.append(f"📦 {plugin_name}")
        lines.extend(f"  • {title}" for title in grouped[plugin_name][:5])
    return "\n".join(lines)


@help_cmd.handle()
async def handle_help(args: Message = CommandArg()) -> None:
    query = args.extract_plain_text().strip()
    engine = get_engine()
    if engine is None:
        await help_cmd.finish("帮助引擎尚未初始化，请稍后再试。")
    if not query:
        await help_cmd.finish(await _build_overview())

    results = await engine.search(query, limit=get_config().default_result_limit)
    if not results:
        await help_cmd.finish("没有找到相关的帮助信息呢……")
    await help_cmd.finish(_format_results(results))


@help_list_cmd.handle()
async def handle_help_list() -> None:
    engine = get_engine()
    if engine is None:
        await help_list_cmd.finish("帮助引擎尚未初始化，请稍后再试。")

    items, total = await engine.list_help(limit=100, offset=0)
    if not items:
        await help_list_cmd.finish("当前还没有可用的帮助条目。")

    lines = [f"📚 当前帮助条目共 {total} 条", "━━━━━━━━━━━━━━━"]
    for item in items:
        prefix = (
            _category_emoji(item.category) if get_config().show_category_emoji else "•"
        )
        suffix = f" ({item.plugin_name})" if item.plugin_name else ""
        lines.append(f"{prefix} {item.title}{suffix}")
    await help_list_cmd.finish("\n".join(lines))


@help_refresh_cmd.handle()
async def handle_help_refresh() -> None:
    engine = get_engine()
    if engine is None:
        await help_refresh_cmd.finish("帮助引擎尚未初始化，请稍后再试。")

    updated_count = await scan_and_sync(engine)
    logger.info("[Komari Help] 手动刷新完成，更新 %s 条帮助条目", updated_count)
    await help_refresh_cmd.finish(
        f"✅ 已重新扫描插件帮助信息，本次同步 {updated_count} 条。"
    )
