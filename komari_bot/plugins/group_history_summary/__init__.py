"""群聊历史总结插件。"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime

from nonebot import logger, on_regex
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment
from nonebot.exception import FinishedException
from nonebot.plugin import PluginMetadata, require

from komari_bot.common.onebot_rules import group_message_to_me_rule
from komari_bot.plugins.komari_decision.services.unified_candidate_rerank import (
    UnifiedCandidateRerankService,
)

from .config_schema import DynamicConfigSchema
from .history_service import check_group_history_supported
from .image_renderer import render_summary_image_base64
from .planner_service import plan_summary_request
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

SUMMARY_TRIGGER_PATTERN = r"(?=.*总结)(?=.*\d).+"
SUMMARY_COUNT_PATTERN = r"总结[^\d]{0,20}(\d{1,4})"
FALLBACK_COUNT_PATTERN = r"(\d{1,4})"
OUT_OF_RANGE_MESSAGE = "我、我只能看10-200条……"
SUMMARY_SCENE_ID = "scene_group_history_summary"

summary_matcher = on_regex(
    r".*总结.*",
    rule=group_message_to_me_rule(),
    priority=9,
    block=True,
)

_group_locks: dict[str, asyncio.Lock] = {}
SUMMARY_TITLE = "小鞠的总结时间到！"
_scene_rerank_service = UnifiedCandidateRerankService()


def _get_group_lock(group_id: str) -> asyncio.Lock:
    lock = _group_locks.get(group_id)
    if lock is None:
        lock = asyncio.Lock()
        _group_locks[group_id] = lock
    return lock


def _extract_requested_count(text: str) -> int | None:
    normalized = " ".join(text.split())
    if "总结" not in normalized:
        return None

    match = re.search(SUMMARY_COUNT_PATTERN, normalized)
    if match is None:
        match = re.search(FALLBACK_COUNT_PATTERN, normalized)
    if match is None:
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

    if re.search(SUMMARY_TRIGGER_PATTERN, normalized):
        return True

    return normalized.startswith(("。", ".", "/"))


def _format_time_range(start_ts: int, end_ts: int) -> str:
    start_str = (
        datetime.fromtimestamp(start_ts, tz=UTC).astimezone().strftime("%m-%d %H:%M")
    )
    end_str = (
        datetime.fromtimestamp(end_ts, tz=UTC).astimezone().strftime("%m-%d %H:%M")
    )
    return f"{start_str} - {end_str}"


async def _is_summary_request(message_text: str) -> bool:
    """结合兜底规则与统一 scene 识别判断是否为总结请求。"""
    normalized = " ".join(message_text.split())
    if "总结" not in normalized:
        return False
    if re.search(SUMMARY_TRIGGER_PATTERN, normalized):
        return True

    try:
        rank_result = await _scene_rerank_service.rank_message(
            normalized, alias_hit=True
        )
    except Exception:
        logger.exception("[GroupHistorySummary] scene 判定失败，回退关键词兜底")
        return False

    return (
        rank_result.best_scene_id == SUMMARY_SCENE_ID
        and rank_result.best_scene_score >= 0.6
        and rank_result.meaningful_score >= rank_result.noise_score
    )


@summary_matcher.handle()
async def handle_group_history_summary(bot: Bot, event: GroupMessageEvent) -> None:
    """处理群聊历史总结请求。"""
    plain_text = event.get_plaintext().strip()
    if not await _is_summary_request(plain_text):
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

    requested_count = _extract_requested_count(plain_text)
    if requested_count is not None and not (
        config.min_summary_count <= requested_count <= config.max_summary_count
    ):
        logger.info(
            "[GroupHistorySummary] 请求条数越界: requested={}, allowed=[{},{}]",
            requested_count,
            config.min_summary_count,
            config.max_summary_count,
        )
        await summary_matcher.finish(OUT_OF_RANGE_MESSAGE)

    is_supported = await check_group_history_supported(bot)
    if not is_supported:
        logger.error(
            "[GroupHistorySummary] 当前 Onebot/Napcat 实现尚未支持获取群聊记录能力"
        )
        return
    try:
        async with group_lock:
            logger.info("[GroupHistorySummary] 正在规划并总结群聊历史...")

            plan_result = await plan_summary_request(
                bot=bot,
                group_id=group_id,
                bot_self_id=str(bot.self_id),
                user_request=plain_text,
                planning_model=config.summary_planning_model,
                planning_max_tokens=config.summary_planning_max_tokens,
                planning_round_limit=config.summary_planning_round_limit,
                summary_default_count=requested_count or config.summary_default_count,
                min_summary_count=config.min_summary_count,
                max_summary_count=config.max_summary_count,
                summary_tool_scan_limit=config.summary_tool_scan_limit,
                fetch_batch_size=config.fetch_batch_size,
            )

            filtered_messages = plan_result.messages

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
            filter_label = "最近消息"
            if plan_result.tool_result is not None:
                filter_label = str(plan_result.tool_result.source)
            subtitle = (
                f"{filter_label} {len(filtered_messages)} 条 | "
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
