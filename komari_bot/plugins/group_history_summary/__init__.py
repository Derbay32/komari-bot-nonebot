"""群聊历史总结插件。"""

from __future__ import annotations

import asyncio
import re
from typing import cast

from nonebot import get_driver, logger, on_regex
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment
from nonebot.exception import FinishedException
from nonebot.matcher import current_matcher
from nonebot.plugin import PluginMetadata, require
from nonebot.typing import T_State  # noqa: TC002 - handler 注解在运行期解析

from komari_bot.decision.dispatch import (
    DecisionDispatch,
    DecisionDispatchUnavailableError,
)
from komari_bot.decision.jev import DecisionResult  # noqa: TC001 - 同上
from komari_bot.onebot import GroupTaskFailureNotification, GroupTaskFailureNotifier
from komari_bot.onebot.onebot_messages import plain_text_message
from komari_bot.onebot.onebot_rules import group_message_to_me_rule
from komari_bot.plugins.komari_decision import (
    get_decision_dispatch,
    get_decision_engine,
)

from .admission_gate import effect_business_admitted
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

require("group_admission")

require("character_binding")
require("komari_decision")

require("komari_context")
from komari_bot.plugins.group_admission import (
    AdmissionQualification,
    adjudicate,
)
from komari_bot.plugins.komari_context import KomariContextConfigSchema

config_manager = config_manager_plugin.get_config_manager(
    "group_history_summary", DynamicConfigSchema
)
# 失败通知开关复用 config_manager 唯一注册表里的 komari_context 资源（不建立
# 平行缓存）；每次通知都读取当前值。
_context_config_manager = config_manager_plugin.get_config_manager(
    "komari_context", KomariContextConfigSchema
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

# 进程级持久失败通知器：跨调用复用同一实例，使 SUPERUSER 私聊的共享冷却
# 在整个进程生命周期内生效；群内固定提示不受冷却影响，仍每次发送。失败通知
# 由 TSK-188 在判定边界提交一次，不再经过旧的场景归类 operation。
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


def _stop_both_paths() -> None:
    """停止本次事件传播（总结命中/失败都不得落入普通聊天）。"""
    current_matcher.get().stop_propagation()


async def _evaluate_summary_candidate(  # noqa: PLR0911 - 每个停止分支都是独立安全停止
    bot: Bot,
    event: GroupMessageEvent,
    state: T_State,
) -> tuple[DecisionDispatch, DecisionResult] | None:
    """消费本次分发唯一的判定结果；不可用时停止两条业务路径。

    返回 ``(dispatch, result)`` 供调用方按路由交接；任何技术失败、取消或缺失
    载体都在这里停止传播并返回 ``None``（绝不本地直通总结）。
    """
    engine = get_decision_engine()
    if engine is None:
        # 没有真实引擎就无法取得有效 Noul：停止两条业务路径。
        logger.info("[GroupHistorySummary] 判定引擎不可用，停止总结与聊天路径")
        _stop_both_paths()
        return None
    try:
        dispatch = get_decision_dispatch(bot, event, state)
    except DecisionDispatchUnavailableError:
        logger.debug("[GroupHistorySummary] 当前分发缺少可用判定载体，停止传播")
        _stop_both_paths()
        return None
    try:
        result = await dispatch.evaluate(engine, summary_candidate=True)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # 只记录固定安全文本与异常类型，不记录正文/提示词/原始异常。
        logger.warning(
            "[GroupHistorySummary] 总结判定未预期失败: error={}",
            type(exc).__name__,
        )
        _stop_both_paths()
        return None
    if result.status == "failed":
        logger.info(
            "[GroupHistorySummary] 总结判定不可用: reason={}",
            result.reason_code,
        )
        # 先停止两条业务路径，再做任何通知 await。
        _stop_both_paths()
        # 失败通知资格与重复事实完全由判定引擎的持久行拥有：领不到资格
        # （重复事件、停用、撤权、中断等既有静默终态）时什么都不发。
        try:
            claimed = await dispatch.claim_handoff("failure_notification")
        except Exception:
            claimed = False
            logger.error("[GroupHistorySummary] 判定失败通知资格领取异常")
        if not claimed:
            logger.debug(
                "[GroupHistorySummary] 失败通知未获准（重复/撤权/静默终态），保持静默: reason={}",
                result.reason_code,
            )
            return None
        # 通知开关复用既有 config_manager 的 komari_context 资源；读取失败只
        # 静默私聊诊断（固定群提示保留，不打印原始异常）。
        notify_superusers = False
        try:
            context_config = cast(
                "KomariContextConfigSchema", _context_config_manager.get()
            )
            notify_superusers = bool(context_config.error_notify_enabled)
        except Exception:
            logger.debug(
                "[GroupHistorySummary] 失败通知开关读取失败，静默 SUPERUSER 私聊"
            )
        summary_enabled = False
        try:
            summary_config = cast("DynamicConfigSchema", config_manager.get())
            summary_enabled = bool(summary_config.plugin_enable)
        except Exception:
            logger.debug("[GroupHistorySummary] 失败通知前总结开关读取失败，抑制通知")
        # 通知前最后复检：总结插件开关与目标群 BUSINESS 准入（受限即抑制）。
        if not summary_enabled or not effect_business_admitted(
            group_id=int(event.group_id)
        ):
            logger.debug("[GroupHistorySummary] 失败通知复检未通过，抑制本次通知")
            return None
        try:
            await _classification_failure_notifier.notify(
                bot=bot,
                notification=GroupTaskFailureNotification(
                    group_id=int(event.group_id),
                    message_id=int(event.message_id),
                    group_text=CLASSIFICATION_FAILURE_MESSAGE,
                    task_kind="group_history_summary",
                    stage="decision",
                    reason_code=result.reason_code,
                    notify_superusers=notify_superusers,
                    request_trace_id=result.decision_id,
                    summary=None,
                ),
            )
        except Exception:
            # 通知边界的意外失败只记录安全诊断：不重试、不进入第二条业务路径。
            logger.error("[GroupHistorySummary] 判定失败通知上报异常")
        return None
    if result.status == "cancelled":
        # 取消/中断不是供应方技术失败：按既有静默终态停止，不提交通知。
        logger.info(
            "[GroupHistorySummary] 总结判定已取消: reason={}",
            result.reason_code,
        )
        _stop_both_paths()
        return None
    return dispatch, result


@summary_matcher.handle()
async def handle_group_history_summary(  # noqa: PLR0911 - 每个门禁都是独立安全停止
    bot: Bot,
    event: GroupMessageEvent,
    state: T_State,
) -> None:
    """处理群聊历史总结请求（消费本次分发唯一的判定结果）。"""
    config = cast("DynamicConfigSchema", config_manager.get())
    group_id = getattr(event, "group_id", None)
    if (
        not config.plugin_enable
        or not isinstance(group_id, int)
        or group_id <= 0
        or adjudicate([group_id]).qualification is not AdmissionQualification.BUSINESS
    ):
        return

    if not await check_group_history_supported(bot):
        logger.info("[GroupHistorySummary] 当前 OneBot 实现不支持群历史，放行消息传播")
        return

    evaluated = await _evaluate_summary_candidate(bot, event, state)
    if evaluated is None:
        return
    dispatch, result = evaluated
    if result.status != "success" or result.route != "summary":
        # 正常未命中（含前置过滤/外部重投/平台召唤直通）：放行传播，
        # 普通聊天入口复用同一份判定结果，绝不再次调用模型或抽签。
        return

    # 总结命中即独占：先停止事件传播，再领取一次性总结交接资格；交接抛出或
    # 被拒都绝不落入普通聊天。
    _stop_both_paths()
    try:
        claimed = await dispatch.claim_handoff("summary")
    except asyncio.CancelledError:
        raise
    except Exception:
        # 交接边界的普通异常就地隔离：只记录固定安全文本，传播保持停止。
        logger.error("[GroupHistorySummary] 总结交接边界异常，保持停止传播")
        return
    if not claimed:
        logger.info("[GroupHistorySummary] 总结交接未获准（重复或撤权），停止传播")
        return

    plain_text = event.get_plaintext().strip()
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
        result_execution = await execute_group_summary(
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

    await _send_group_summary_outputs(bot, event, result_execution)


async def _send_group_summary_outputs(
    bot: Bot,
    event: GroupMessageEvent,
    result: SummaryExecutionResult,
) -> None:
    """向目标群逐条发布总结正文/图片分段，每条输出前按目标群裁决。

    TSK-229：每条群输出都是独立的不可分瞬时效果（摘要.group_output），在
    bot.send 之前按目标群 BUSINESS 裁决；受限时正常丢弃该条输出，不发送、
    不通知、不复活，不影响其余已获准分段。用于（1）无图时正文文本发布，
    （2）有图时逐张图片发布。
    """
    if not result.image_base64:
        if effect_business_admitted(group_id=int(event.group_id)):
            await bot.send(
                event,
                plain_text_message(result.summary_text),
            )
        return

    image_pages = getattr(result, "image_pages_base64", ()) or (result.image_base64,)
    for image_page in image_pages:
        if effect_business_admitted(group_id=int(event.group_id)):
            await bot.send(
                event,
                MessageSegment.image(file=f"base64://{image_page}"),
            )
        else:
            logger.info(
                "[GroupHistorySummary] 群输出准入受限，丢弃该条分段: group_id={}",
                event.group_id,
            )
