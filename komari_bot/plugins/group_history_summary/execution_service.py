"""群聊历史总结共享执行服务，供正常 handler 与 debug 插件共同调用。"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nonebot import logger

from .history_service import check_group_history_supported
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


@dataclass(slots=True)
class SummaryExecutionResult:
    """总结执行结构化的返回结果。"""

    summary_text: str
    filtered_message_count: int
    plan_result: SummaryPlanResult
    image_base64: str
    filter_label: str
    time_range: str


SUMMARY_TITLE = "小鞠的总结时间到！"
EMPTY_HISTORY_TEXT = "可用的文本记录太少，没法总结……"

_group_locks: dict[str, asyncio.Lock] = {}
_running_groups: set[str] = set()


def _get_group_lock(group_id: str) -> asyncio.Lock:
    lock = _group_locks.get(group_id)
    if lock is None:
        lock = asyncio.Lock()
        _group_locks[group_id] = lock
    return lock


def _format_time_range(start_ts: int, end_ts: int) -> str:
    from datetime import UTC, datetime

    start_str = (
        datetime.fromtimestamp(start_ts, tz=UTC).astimezone().strftime("%m-%d %H:%M")
    )
    end_str = (
        datetime.fromtimestamp(end_ts, tz=UTC).astimezone().strftime("%m-%d %H:%M")
    )
    return f"{start_str} - {end_str}"


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
    """
    if group_id in _running_groups:
        raise SummaryBusyError("在、在看了……等、等会！")
    _running_groups.add(group_id)

    try:
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

        async with _get_group_lock(group_id):
            plan_result = await plan_summary_request(
                bot=bot,
                group_id=group_id,
                bot_self_id=bot_self_id,
                user_request=user_request,
                planning_model=config.summary_planning_model,
                planning_max_tokens=config.summary_planning_max_tokens,
                planning_round_limit=config.summary_planning_round_limit,
                summary_default_count=(
                    requested_count or config.summary_default_count
                ),
                min_summary_count=config.min_summary_count,
                max_summary_count=config.max_summary_count,
                summary_tool_scan_limit=config.summary_tool_scan_limit,
                fetch_batch_size=config.fetch_batch_size,
                planning_thinking_mode=config.summary_planning_thinking_mode,
                planning_reasoning_effort=config.summary_planning_reasoning_effort,
                request_trace_id=request_trace_id,
                collector=collector,
            )

            filtered_messages = plan_result.messages

            if not filtered_messages:
                logger.info("[GroupHistorySummary] 没有可用的历史消息")
                return SummaryExecutionResult(
                    summary_text=EMPTY_HISTORY_TEXT,
                    filtered_message_count=0,
                    plan_result=plan_result,
                    image_base64="",
                    filter_label="无",
                    time_range="无",
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

            return SummaryExecutionResult(
                summary_text=summary_text,
                filtered_message_count=len(filtered_messages),
                plan_result=plan_result,
                image_base64=image_base64,
                filter_label=filter_label,
                time_range=_format_time_range(
                    filtered_messages[0].timestamp,
                    filtered_messages[-1].timestamp,
                ),
            )
    finally:
        _running_groups.discard(group_id)
