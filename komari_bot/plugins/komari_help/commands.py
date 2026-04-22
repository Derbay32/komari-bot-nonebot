"""Komari Help 命令处理器。"""

from __future__ import annotations

from collections import defaultdict

from nonebot import logger, on_command
from nonebot.adapters.onebot.v11 import Message  # noqa: TC002
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER

from .engine import get_engine
from .rendering import (
    LIST_PAGE_SIZE,
    format_list_page,
    format_results,
    get_search_result_limit,
)
from .scanner import scan_and_sync

help_cmd = on_command("docs", aliases={"帮助"}, priority=10, block=True)
help_list_cmd = on_command(("docs", "list"), priority=9, block=True)
help_refresh_cmd = on_command(
    ("docs", "refresh"),
    permission=SUPERUSER,
    priority=5,
    block=True,
)


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
        await help_cmd.finish(
            "请简单描述你需要查询的指令（如指令本体、或指令能做什么）"
        )

    results = await engine.search(query, limit=get_search_result_limit())
    if not results:
        await help_cmd.finish("没有找到相关的帮助信息呢……")
    await help_cmd.finish(format_results(results))


@help_list_cmd.handle()
async def handle_help_list(args: Message = CommandArg()) -> None:
    engine = get_engine()
    if engine is None:
        await help_list_cmd.finish("帮助引擎尚未初始化，请稍后再试。")

    raw_page = args.extract_plain_text().strip()
    page = 1
    if raw_page:
        try:
            page = int(raw_page)
        except ValueError:
            await help_list_cmd.finish("页码必须是正整数。")
        if page < 1:
            await help_list_cmd.finish("页码必须是正整数。")

    items, total = await engine.list_help(
        limit=LIST_PAGE_SIZE,
        offset=(page - 1) * LIST_PAGE_SIZE,
    )
    if not items:
        if total > 0:
            total_pages = (total + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE
            await help_list_cmd.finish(f"第 {page} 页不存在，当前共 {total_pages} 页。")
        await help_list_cmd.finish("当前还没有可用的帮助条目。")

    await help_list_cmd.finish(format_list_page(items, total, page))


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
