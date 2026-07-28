"""群聊历史总结插件。"""

from __future__ import annotations

import re

from nonebot import get_driver, logger, on_regex
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment
from nonebot.exception import FinishedException
from nonebot.matcher import current_matcher
from nonebot.plugin import PluginMetadata, require

from komari_bot.common.onebot_messages import plain_text_message
from komari_bot.common.onebot_rules import group_message_to_me_rule

from .config_schema import DynamicConfigSchema
from .execution_service import (
    CapabilityNotSupportedError,
    HistoryIncompleteError,
    SummaryBusyError,
    SummaryServiceUnavailableError,
    execute_group_summary,
)
from .execution_service import (
    SummaryExecutionResult as SummaryExecutionResult,
)
from .group_lock import close_group_summary_lock_manager
from .history_service import check_group_history_supported

config_manager_plugin = require("config_manager")
agent_run_logger_plugin = require("agent_run_logger")
permission_manager_plugin = require("permission_manager")
character_binding = require("character_binding")
komari_decision_plugin = require("komari_decision")

UnifiedCandidateRerankService = komari_decision_plugin.UnifiedCandidateRerankService

config_manager = config_manager_plugin.get_config_manager(
    "group_history_summary", DynamicConfigSchema
)

__plugin_meta__ = PluginMetadata(
    name="group_history_summary",
    description="@机器人并要求\u201c总结过去XX条\u201d时，拉群历史消息并生成图文总结",
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
    block=False,
)

_scene_rerank_service = UnifiedCandidateRerankService()
try:
    driver = get_driver()
except ValueError:
    driver = None

if driver is not None:

    @driver.on_shutdown
    async def _close_group_summary_resources() -> None:
        """关闭群总结分布式锁连接。"""
        await close_group_summary_lock_manager()


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

    logger.info(
        "[SummaryCheck] rerank结果: best_scene={}, score={:.4f}, "
        "meaningful={:.4f}, noise={:.4f}",
        rank_result.best_scene_id,
        rank_result.best_scene_score,
        rank_result.meaningful_score,
        rank_result.noise_score,
    )

    is_summary_request = False
    if rank_result.best_scene_id != SUMMARY_SCENE_ID:
        logger.info(
            "[SummaryCheck] 失败: best_scene_id={} (期望={})",
            rank_result.best_scene_id,
            SUMMARY_SCENE_ID,
        )
    elif rank_result.best_scene_score < 0.6:
        logger.info(
            "[SummaryCheck] 失败: best_scene_score={:.4f} < 0.6",
            rank_result.best_scene_score,
        )
    else:
        logger.info("[SummaryCheck] 全部条件满足，确认为总结请求")
        is_summary_request = True

    return is_summary_request


@summary_matcher.handle()
async def handle_group_history_summary(
    bot: Bot,
    event: GroupMessageEvent,
) -> None:
    """处理群聊历史总结请求。"""
    config = config_manager.get()
    if not config.plugin_enable:
        return

    can_use, _ = await permission_manager_plugin.check_runtime_permission(
        bot, event, config
    )
    if not can_use:
        return

    if not await check_group_history_supported(bot):
        logger.info(
            "[GroupHistorySummary] 当前 OneBot 实现不支持群历史，放行消息传播"
        )
        return

    plain_text = event.get_plaintext().strip()
    if not await _is_summary_request(plain_text):
        return

    current_matcher.get().stop_propagation()

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

    try:
        result = await execute_group_summary(
            bot=bot,
            group_id=str(event.group_id),
            bot_self_id=str(bot.self_id),
            user_request=plain_text,
            config=config,
            requested_count=requested_count,
            history_capability_confirmed=True,
        )
    except SummaryBusyError as exc:
        await summary_matcher.finish(plain_text_message(exc))
    except HistoryIncompleteError:
        await summary_matcher.finish("群历史记录没能完整取回，暂时不能可靠地总结……")
    except SummaryServiceUnavailableError as exc:
        await summary_matcher.finish(plain_text_message(exc))
    except CapabilityNotSupportedError:
        return
    except FinishedException:
        raise
    except Exception:
        logger.exception("[GroupHistorySummary] 处理总结请求失败")
        return

    if not result.image_base64:
        await summary_matcher.finish(plain_text_message(result.summary_text))

    image_pages = getattr(result, "image_pages_base64", ()) or (result.image_base64,)
    for image_page in image_pages:
        await bot.send(
            event,
            MessageSegment.image(file=f"base64://{image_page}"),
        )
