"""Komari Help 命令处理器。"""

from __future__ import annotations

from collections import defaultdict

from nonebot import logger, on_command
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent  # noqa: TC002
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.plugin import require

from komari_bot.onebot.onebot_messages import plain_text_message
from komari_bot.plugins.group_admission import AdmissionQualification, adjudicate

from .config_schema import DynamicConfigSchema
from .engine import get_engine
from .rendering import (
    LIST_PAGE_SIZE,
    format_list_page,
    format_results,
    get_search_result_limit,
)
from .scanner import HelpScanAlreadyRunningError, scan_and_sync

require("config_manager")
require("group_admission")

from komari_bot.plugins import config_manager as config_manager_plugin

config_manager = config_manager_plugin.get_config_manager(
    "komari_help",
    DynamicConfigSchema,
)

help_cmd = on_command("docs", aliases={"帮助"}, priority=10, block=True)
help_list_cmd = on_command(("docs", "list"), priority=9, block=True)
help_refresh_cmd = on_command(
    ("docs", "refresh"),
    priority=5,
    block=True,
)


async def _admission_gate(event: MessageEvent) -> bool:
    """统一准入复查与本插件自有开关的前置门。

    先由 ``group_admission`` 裁决关联群，获准后读取本插件自身
    ``plugin_enable`` 进一步收窄；任一环节不满足都静默跳过。``plugin_enable``
    只收窄不扩张：准入资格始终由裁决决定。
    """
    group_id = getattr(event, "group_id", None)
    if not isinstance(group_id, int) or group_id <= 0:
        return False
    if adjudicate([group_id]).qualification is not AdmissionQualification.BUSINESS:
        return False
    config = await config_manager.get_async()
    return bool(getattr(config, "plugin_enable", True))


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
async def handle_help(
    event: MessageEvent,
    args: Message = CommandArg(),
) -> None:
    # 统一准入复查与插件开关前置门：不通过则静默跳过
    if not await _admission_gate(event):
        return

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
    await help_cmd.finish(plain_text_message(format_results(results)))


@help_list_cmd.handle()
async def handle_help_list(
    event: MessageEvent,
    args: Message = CommandArg(),
) -> None:
    # 统一准入复查与前置开关门：不通过则静默跳过
    if not await _admission_gate(event):
        return

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
            await help_list_cmd.finish(
                plain_text_message(f"第 {page} 页不存在，当前共 {total_pages} 页。")
            )
        await help_list_cmd.finish("当前还没有可用的帮助条目。")

    await help_list_cmd.finish(
        plain_text_message(format_list_page(items, total, page))
    )


@help_refresh_cmd.handle()
async def handle_help_refresh(bot: Bot, event: MessageEvent) -> None:
    if not await SUPERUSER(bot, event):
        await help_refresh_cmd.finish("❌ 仅限 SUPERUSER 使用")

    # 统一准入复查与前置开关门：不通过则静默跳过
    if not await _admission_gate(event):
        return

    engine = get_engine()
    if engine is None:
        await help_refresh_cmd.finish("帮助引擎尚未初始化，请稍后再试。")

    try:
        updated_count = await scan_and_sync(engine)
    except HelpScanAlreadyRunningError:
        await help_refresh_cmd.finish("⏳ 另一个进程正在扫描帮助信息，请稍后再试。")
    logger.info("[Komari Help] 手动刷新完成，更新 %s 条帮助条目", updated_count)
    await help_refresh_cmd.finish(
        plain_text_message(f"✅ 已重新扫描插件帮助信息，本次同步 {updated_count} 条。")
    )
