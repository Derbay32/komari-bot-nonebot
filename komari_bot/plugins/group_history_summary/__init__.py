"""群聊历史总结插件。"""

from __future__ import annotations

import re
from typing import cast

from nonebot import get_driver, logger, on_regex
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment
from nonebot.exception import FinishedException
from nonebot.matcher import current_matcher
from nonebot.plugin import PluginMetadata, require

from komari_bot.decision import (
    SummaryRequestClassificationResult,
    SummaryRequestClassificationStatus,
    SummaryRequestUnavailableReason,
)
from komari_bot.onebot import GroupTaskFailureNotification, GroupTaskFailureNotifier
from komari_bot.onebot.onebot_messages import plain_text_message
from komari_bot.onebot.onebot_rules import group_message_to_me_rule
from komari_bot.plugins.komari_decision import classify_summary_request

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

require("config_manager")
from komari_bot.plugins import config_manager as config_manager_plugin

require("agent_run_logger")
require("permission_manager")
from komari_bot.plugins import permission_manager as permission_manager_plugin

require("character_binding")
require("komari_decision")

config_manager = config_manager_plugin.get_config_manager(
    "group_history_summary", DynamicConfigSchema
)

__plugin_meta__ = PluginMetadata(
    name="group_history_summary",
    description="@机器人并要求\u201c总结过去XX条\u201d时，拉群历史消息并生成图文总结",
    usage="@机器人 总结过去50条",
)

SUMMARY_COUNT_PATTERN = r"总结[^\d]{0,20}(\d{1,4})"
FALLBACK_COUNT_PATTERN = r"(\d{1,4})"
OUT_OF_RANGE_MESSAGE = "我、我只能看10-200条……"
# 场景归类阶段失败时发送的固定群提示；不得携带异常、场景键、分数、阈值或配置。
CLASSIFICATION_FAILURE_MESSAGE = "群聊总结暂时不可用，稍后再试试吧……"

# 静默私聊通知的安全原因码：仍发送固定群提示，但不向 SUPERUSER 发送诊断。
_SILENT_NOTIFY_REASONS: frozenset[SummaryRequestUnavailableReason] = frozenset(
    {
        SummaryRequestUnavailableReason.DECISION_DISABLED,
        SummaryRequestUnavailableReason.RERANK_UNAVAILABLE,
    }
)

# 进程级持久失败通知器：跨调用复用同一实例，使 SUPERUSER 私聊的共享冷却
# 在整个进程生命周期内生效；群内固定提示不受冷却影响，仍每次发送。
_classification_failure_notifier = GroupTaskFailureNotifier()

summary_matcher = on_regex(
    r".*总结.*",
    rule=group_message_to_me_rule(),
    priority=9,
    block=False,
)

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


def _classification_notification(
    *,
    group_id: int,
    message_id: int,
    reason_code: str,
    notify_superusers: bool,
) -> GroupTaskFailureNotification:
    """构造场景归类阶段失败通知（固定任务类型、阶段与群提示）。"""
    return GroupTaskFailureNotification(
        group_id=group_id,
        message_id=message_id,
        group_text=CLASSIFICATION_FAILURE_MESSAGE,
        task_kind="group_history_summary",
        stage="scene_classification",
        reason_code=reason_code,
        notify_superusers=notify_superusers,
        request_trace_id=None,
        summary=None,
    )


async def _notify_classification_failure(
    bot: Bot,
    event: GroupMessageEvent,
    *,
    reason_code: str,
    notify_superusers: bool,
) -> None:
    """投递场景归类失败通知；群内 reply 段由通知边界自身负责。

    复用模块级持久通知器实例，保证 SUPERUSER 私聊的共享冷却在进程内
    持续生效；群内固定提示仍每次发送。
    """
    await _classification_failure_notifier.notify(
        bot=bot,
        notification=_classification_notification(
            group_id=int(event.group_id),
            message_id=int(event.message_id),
            reason_code=reason_code,
            notify_superusers=notify_superusers,
        ),
    )


async def _classify_and_route(
    bot: Bot,
    event: GroupMessageEvent,
    plain_text: str,
) -> bool:
    """调用判定插件顶层归类 operation 并按窄结果路由。

    返回 True 表示命中（MATCHED）总结请求，调用方应继续执行总结；
    未命中（NOT_MATCHED）放行：不停止传播、不通知、不执行总结；
    不可用（UNAVAILABLE）记录日志、停止传播并发送固定群提示，按安全原因码
    决定是否私聊 SUPERUSER；未预期异常记录异常、停止传播并通知 SUPERUSER。
    """
    try:
        classification: SummaryRequestClassificationResult = (
            await classify_summary_request(plain_text)
        )
    except Exception:
        logger.exception("[GroupHistorySummary] 场景归类发生未预期异常，停止传播")
        current_matcher.get().stop_propagation()
        await _notify_classification_failure(
            bot,
            event,
            reason_code="unexpected_error",
            notify_superusers=True,
        )
        return False

    if classification.status is SummaryRequestClassificationStatus.NOT_MATCHED:
        return False

    if classification.status is SummaryRequestClassificationStatus.UNAVAILABLE:
        reason = cast("SummaryRequestUnavailableReason", classification.reason)
        if reason is SummaryRequestUnavailableReason.DECISION_DISABLED:
            logger.info(
                "[GroupHistorySummary] 场景归类不可用: reason={}",
                reason.value,
            )
        else:
            # RERANK_UNAVAILABLE 等未达失败预算的预期故障只记 warning，
            # 日志仅含稳定原因码，不携带异常、场景键、分数、阈值或配置。
            logger.warning(
                "[GroupHistorySummary] 场景归类不可用: reason={}",
                reason.value,
            )
        current_matcher.get().stop_propagation()
        await _notify_classification_failure(
            bot,
            event,
            reason_code=reason.value,
            notify_superusers=reason not in _SILENT_NOTIFY_REASONS,
        )
        return False

    return True


@summary_matcher.handle()
async def handle_group_history_summary(
    bot: Bot,
    event: GroupMessageEvent,
) -> None:
    """处理群聊历史总结请求。"""
    config = cast("DynamicConfigSchema", config_manager.get())
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
    if not await _classify_and_route(bot, event, plain_text):
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
