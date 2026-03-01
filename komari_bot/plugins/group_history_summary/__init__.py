"""群聊历史总结插件。"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime

from nonebot import logger, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment
from nonebot.exception import FinishedException
from nonebot.plugin import PluginMetadata, require

from .config_schema import DynamicConfigSchema
from .history_service import check_group_history_supported, fetch_group_history_messages
from .image_renderer import render_summary_image_base64
from .summarize_service import summarize_history_messages, summary_text_to_lines

config_manager_plugin = require("config_manager")
permission_manager_plugin = require("permission_manager")
character_binding = require("character_binding")

config_manager = config_manager_plugin.get_config_manager(
    "group_history_summary", DynamicConfigSchema
)

__plugin_meta__ = PluginMetadata(
    name="group_history_summary",
    description="@机器人并要求“总结过去XX条”时，拉群历史消息并生成图文总结",
    usage="@机器人 总结过去50条",
)

summary_matcher = on_message(priority=9, block=False)

SUMMARY_PATTERN = re.compile(r"总结(?:过去)?\s*(\d{1,4})\s*条")

_group_locks: dict[str, asyncio.Lock] = {}
SUMMARY_TITLE = "小鞠的总结时间到！"


def _get_group_lock(group_id: str) -> asyncio.Lock:
    lock = _group_locks.get(group_id)
    if lock is None:
        lock = asyncio.Lock()
        _group_locks[group_id] = lock
    return lock


def _extract_requested_count(text: str) -> int | None:
    match = SUMMARY_PATTERN.search(text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _format_time_range(start_ts: int, end_ts: int) -> str:
    start_str = (
        datetime.fromtimestamp(start_ts, tz=UTC).astimezone().strftime("%m-%d %H:%M")
    )
    end_str = (
        datetime.fromtimestamp(end_ts, tz=UTC).astimezone().strftime("%m-%d %H:%M")
    )
    return f"{start_str} - {end_str}"


@summary_matcher.handle()
async def handle_group_history_summary(bot: Bot, event: GroupMessageEvent) -> None:
    """处理 @机器人 总结过去XX条。"""
    if not event.to_me:
        return

    plain_text = event.get_plaintext().strip()
    requested_count = _extract_requested_count(plain_text)
    if requested_count is None:
        return

    config = config_manager.get()
    can_use, reason = await permission_manager_plugin.check_runtime_permission(
        bot, event, config
    )
    if not can_use:
        await summary_matcher.finish(f"❌ {reason}")

    group_id = str(event.group_id)
    group_lock = _get_group_lock(group_id)
    if group_lock.locked():
        await summary_matcher.finish("在、在看了……等、等会！")

    count = max(
        config.min_summary_count, min(requested_count, config.max_summary_count)
    )
    if count != requested_count:
        logger.warning(f"[GroupHistorySummary] 已将条数调整为 {count} 条。")

    is_supported = await check_group_history_supported(bot)
    if not is_supported:
        logger.error(
            "[GroupHistorySummary] 当前 Onebot/Napcat 实现尚未支持获取群聊记录能力"
        )
        return
    try:
        async with group_lock:
            logger.info(f"[GroupHistorySummary] 正在读取并总结最近 {count} 条消息...")

            history_messages = await fetch_group_history_messages(
                bot=bot,
                group_id=group_id,
                count=count,
                batch_size=config.fetch_batch_size,
                name_resolver=character_binding.get_character_name,
            )

            if not history_messages:
                logger.info("[GroupHistorySummary] 没有可用的历史消息")
                await summary_matcher.finish("没、没有消息啊……？")

            summary_text = await summarize_history_messages(
                history_messages=history_messages,
                model=config.summary_model,
                temperature=config.summary_temperature,
                max_tokens=config.summary_max_tokens,
            )

            body_lines = summary_text_to_lines(summary_text)
            subtitle = (
                f"最近 {len(history_messages)} 条 | "
                f"{_format_time_range(history_messages[0].timestamp, history_messages[-1].timestamp)}"
            )
            image_base64 = render_summary_image_base64(
                title=SUMMARY_TITLE,
                subtitle=subtitle,
                body_lines=body_lines,
                width=config.card_width,
                font_size=config.card_font_size,
            )

            await bot.send(
                event,
                MessageSegment.image(file=f"base64://{image_base64}"),
            )

    except FinishedException:
        raise
    except Exception:
        logger.exception("[GroupHistorySummary] 处理总结请求失败")
        return
