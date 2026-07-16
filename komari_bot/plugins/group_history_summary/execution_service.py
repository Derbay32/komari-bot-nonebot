"""群聊历史总结共享执行服务，供正常 handler 与 debug 插件共同调用。"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nonebot import logger

from .group_lock import GroupSummaryLockLostError, group_summary_lock_manager
from .history_service import (
    HistoryFetchMetadata,
    HistoryIncompleteError,
    check_group_history_supported,
)
from .image_renderer import render_summary_image_base64
from .planner_service import SummaryPlanResult, plan_summary_request
from .summarize_service import summarize_history_messages, summary_text_to_lines

if TYPE_CHECKING:
    from nonebot.adapters.onebot.v11 import Bot

    from komari_bot.plugins.llm_provider.diagnostic import LLMDiagnosticCollector

    from .config_schema import DynamicConfigSchema


class SummaryBusyError(Exception):
    """同群已有总结任务运行中。"""


class CapabilityNotSupportedError(Exception):
    """当前 OneBot 实现不支持获取群聊记录。"""


class SummaryServiceUnavailableError(RuntimeError):
    """群总结分布式锁服务暂不可用或执行中失去租约。"""


@dataclass(slots=True)
class SummaryExecutionResult:
    """总结执行结构化的返回结果。"""

    summary_text: str
    filtered_message_count: int
    plan_result: SummaryPlanResult
    image_base64: str
    filter_label: str
    time_range: str
    history_fetch: HistoryFetchMetadata | None


SUMMARY_TITLE = "小鞠的总结时间到！"
EMPTY_HISTORY_TEXT = "可用的文本记录太少，没法总结……"

_group_lock_manager = group_summary_lock_manager


def _format_time_range(start_ts: int, end_ts: int) -> str:
    from datetime import UTC, datetime

    start_str = (
        datetime.fromtimestamp(start_ts, tz=UTC).astimezone().strftime("%m-%d %H:%M")
    )
    end_str = (
        datetime.fromtimestamp(end_ts, tz=UTC).astimezone().strftime("%m-%d %H:%M")
    )
    return f"{start_str} - {end_str}"


def _get_history_fetch_metadata(
    plan_result: SummaryPlanResult,
) -> HistoryFetchMetadata | None:
    tool_result = plan_result.tool_result
    if tool_result is None:
        return None
    metadata = getattr(tool_result, "history_fetch", None)
    return metadata if isinstance(metadata, HistoryFetchMetadata) else None


def _format_partial_history_notice(metadata: HistoryFetchMetadata | None) -> str:
    if metadata is None or metadata.status != "partial":
        return ""
    failed_batch = metadata.failed_batch or metadata.completed_batches + 1
    missing_suffix = (
        f"，约缺 {metadata.missing_count} 条记录" if metadata.missing_count > 0 else ""
    )
    return (
        f"⚠ 历史第 {failed_batch} 批读取失败{missing_suffix}；"
        f"以下基于已取回的 {metadata.retrieved_item_count} 条记录。"
    )


async def _execute_group_summary_core(
    *,
    bot: "Bot",
    group_id: str,
    bot_self_id: str,
    user_request: str,
    config: DynamicConfigSchema,
    requested_count: int | None,
    collector: "LLMDiagnosticCollector | None",
    history_capability_confirmed: bool,
) -> SummaryExecutionResult:
    """在已持有群租约时执行规划、总结和渲染。"""
    is_supported = history_capability_confirmed
    if not is_supported:
        is_supported = await check_group_history_supported(bot)
    if not is_supported:
        logger.error(
            "[GroupHistorySummary] 当前 Onebot/Napcat 实现尚未支持获取群聊记录能力"
        )
        raise CapabilityNotSupportedError

    request_trace_id = (
        collector.request_id if collector is not None else uuid.uuid4().hex[:12]
    )

    logger.info("[GroupHistorySummary] 开始执行总结，trace_id={}", request_trace_id)

    plan_result = await plan_summary_request(
        bot=bot,
        group_id=group_id,
        bot_self_id=bot_self_id,
        user_request=user_request,
        planning_model=config.summary_planning_model,
        planning_max_tokens=config.summary_planning_max_tokens,
        planning_round_limit=config.summary_planning_round_limit,
        summary_default_count=(requested_count or config.summary_default_count),
        min_summary_count=config.min_summary_count,
        max_summary_count=config.max_summary_count,
        summary_tool_scan_limit=config.summary_tool_scan_limit,
        fetch_batch_size=config.fetch_batch_size,
        planning_thinking_mode=config.summary_planning_thinking_mode,
        planning_reasoning_effort=config.summary_planning_reasoning_effort,
        request_trace_id=request_trace_id,
        collector=collector,
        history_min_coverage_ratio=config.history_min_coverage_ratio,
    )

    filtered_messages = plan_result.messages
    history_fetch = _get_history_fetch_metadata(plan_result)
    partial_notice = _format_partial_history_notice(history_fetch)

    if not filtered_messages:
        logger.info("[GroupHistorySummary] 没有可用的历史消息")
        empty_text = (
            f"{partial_notice}\n{EMPTY_HISTORY_TEXT}"
            if partial_notice
            else EMPTY_HISTORY_TEXT
        )
        return SummaryExecutionResult(
            summary_text=empty_text,
            filtered_message_count=0,
            plan_result=plan_result,
            image_base64="",
            filter_label="无",
            time_range="无",
            history_fetch=history_fetch,
        )

    summary_text = await summarize_history_messages(
        history_messages=filtered_messages,
        model=config.summary_model,
        temperature=config.summary_temperature,
        max_tokens=config.summary_max_tokens,
        assistant_prefill_enabled=config.assistant_prefill_enabled,
        dsv4_roleplay_instruct_mode=config.dsv4_roleplay_instruct_mode,
        thinking_mode=config.summary_thinking_mode,
        reasoning_effort=config.summary_reasoning_effort,
        request_trace_id=request_trace_id,
        collector=collector,
    )

    result_summary_text = (
        f"{partial_notice}\n{summary_text}" if partial_notice else summary_text
    )
    body_lines = summary_text_to_lines(result_summary_text)
    filter_label = "最近消息"
    if plan_result.tool_result is not None:
        filter_label = str(plan_result.tool_result.source)
    time_range = _format_time_range(
        filtered_messages[0].timestamp,
        filtered_messages[-1].timestamp,
    )
    subtitle = f"{filter_label} {len(filtered_messages)} 条 | {time_range}"
    image_base64 = await asyncio.to_thread(
        render_summary_image_base64,
        title=SUMMARY_TITLE,
        subtitle=subtitle,
        body_lines=body_lines,
        layout_params=config.layout_params.model_dump(),
    )

    return SummaryExecutionResult(
        summary_text=result_summary_text,
        filtered_message_count=len(filtered_messages),
        plan_result=plan_result,
        image_base64=image_base64,
        filter_label=filter_label,
        time_range=time_range,
        history_fetch=history_fetch,
    )


async def execute_group_summary(
    bot: "Bot",
    group_id: str,
    bot_self_id: str,
    user_request: str,
    config: DynamicConfigSchema,
    requested_count: int | None = None,
    collector: "LLMDiagnosticCollector | None" = None,
    *,
    history_capability_confirmed: bool = False,
) -> SummaryExecutionResult:
    """群聊历史总结的共享执行入口。

    由正常 handler 与 debug 插件共同调用，内部处理能力检查、群锁、
    规划、总结和图片渲染。正常入口已完成能力检查时，可通过
    ``history_capability_confirmed`` 避免对同一请求重复探测。

    Raises:
        SummaryBusyError: 同群已有总结任务运行中
        CapabilityNotSupportedError: 当前 OneBot 实现不支持群历史 API
        HistoryIncompleteError: 群历史读取失败且完整度低于配置阈值
        SummaryServiceUnavailableError: 分布式锁服务不可用或租约丢失
    """
    try:
        lease = await _group_lock_manager.try_acquire(
            group_id=group_id,
            redis_db=config.redis_db,
            ttl_seconds=config.summary_lock_ttl_seconds,
        )
    except Exception as exc:
        logger.error(
            "[GroupHistorySummary] 分布式锁获取失败: error_type={}",
            type(exc).__name__,
        )
        raise SummaryServiceUnavailableError(
            "群总结服务暂时不可用，请稍后重试"
        ) from None

    if lease is None:
        raise SummaryBusyError("在、在看了……等、等会！")

    try:
        try:
            return await lease.run(
                _execute_group_summary_core(
                    bot=bot,
                    group_id=group_id,
                    bot_self_id=bot_self_id,
                    user_request=user_request,
                    config=config,
                    requested_count=requested_count,
                    collector=collector,
                    history_capability_confirmed=history_capability_confirmed,
                )
            )
        except GroupSummaryLockLostError:
            raise SummaryServiceUnavailableError(
                "群总结任务租约已失效，请稍后重试"
            ) from None
    finally:
        await lease.close()


__all__ = [
    "CapabilityNotSupportedError",
    "HistoryIncompleteError",
    "SummaryBusyError",
    "SummaryExecutionResult",
    "SummaryServiceUnavailableError",
    "execute_group_summary",
]
