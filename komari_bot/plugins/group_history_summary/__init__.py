"""群聊历史总结插件。"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime

from nonebot import logger, on_regex
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment
from nonebot.exception import FinishedException
from nonebot.plugin import PluginMetadata, require
from nonebot.rule import to_me

from .config_schema import DynamicConfigSchema
from .history_service import (
    HistoryMessage,
    check_group_history_supported,
    fetch_group_history_messages,
)
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

SUMMARY_PATTERN = r"总结(?:过去)?\s*(\d{1,4})\s*条"
summary_matcher = on_regex(
    SUMMARY_PATTERN,
    rule=to_me(),
    priority=9,
    block=True,
)

_group_locks: dict[str, asyncio.Lock] = {}
SUMMARY_TITLE = "小鞠的总结时间到！"


def _get_group_lock(group_id: str) -> asyncio.Lock:
    lock = _group_locks.get(group_id)
    if lock is None:
        lock = asyncio.Lock()
        _group_locks[group_id] = lock
    return lock


def _extract_requested_count(text: str) -> int | None:
    match = re.search(SUMMARY_PATTERN, text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _is_command_message(text: str) -> bool:
    """判断是否为命令文本。

    规则：
    1. 匹配“总结过去XX条 / 总结XX条”
    2. 以 。/.// 开头（如 .jrhg、/bind、。help）
    """
    normalized = "".join(text.split())
    if not normalized:
        return False

    if re.search(SUMMARY_PATTERN, normalized):
        return True

    return normalized.startswith(("。", ".", "/"))


def _filter_messages_for_summary(
    messages: list[HistoryMessage],
    bot_self_id: str,
) -> list[HistoryMessage]:
    """过滤命令消息，以及机器人对这些命令的回复。"""
    # 第一层：命令文本直接剔除（不论是谁发的）
    command_indexes = {
        idx for idx, message in enumerate(messages) if _is_command_message(message.content)
    }
    command_message_ids = {
        message.message_id
        for idx, message in enumerate(messages)
        if idx in command_indexes and message.message_id
    }

    # 第二层：只剔除“机器人 reply 到命令消息”的回复。
    bot_reply_indexes: set[int] = set()
    if command_message_ids:
        for idx, message in enumerate(messages):
            if message.user_id != bot_self_id:
                continue
            if not message.reply_to_message_id:
                continue
            if message.reply_to_message_id in command_message_ids:
                bot_reply_indexes.add(idx)

    filtered: list[HistoryMessage] = []
    for idx, message in enumerate(messages):
        if idx in command_indexes:
            continue
        if idx in bot_reply_indexes:
            continue
        filtered.append(message)
    return filtered


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

            filtered_messages = _filter_messages_for_summary(
                messages=history_messages,
                bot_self_id=str(bot.self_id),
            )

            if not filtered_messages:
                logger.info("[GroupHistorySummary] 没有可用的历史消息")
                await summary_matcher.finish("可用的文本记录太少，没法总结……")

            summary_text = await summarize_history_messages(
                history_messages=filtered_messages,
                model=config.summary_model,
                temperature=config.summary_temperature,
                max_tokens=config.summary_max_tokens,
            )

            body_lines = summary_text_to_lines(summary_text)
            subtitle = (
                f"最近 {len(filtered_messages)} 条 | "
                f"{_format_time_range(filtered_messages[0].timestamp, filtered_messages[-1].timestamp)}"
            )
            image_base64 = render_summary_image_base64(
                title=SUMMARY_TITLE,
                subtitle=subtitle,
                body_lines=body_lines,
                layout_params=config.layout_params.model_dump(),
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
