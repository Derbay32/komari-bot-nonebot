"""群聊历史总结共享执行服务，供正常 handler 与 debug 插件共同调用。

TSK-206：Python 冻结预算与历史限制，执行一个 ``TaskKind.SUMMARY`` DSH 任务；
模型通过受限历史工具读取结构化数据，并通过 ``final_summary`` 提交最终正文。
Python 继续负责群锁、能力检查、准入、结果校验、渲染与调用方输出。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nonebot import logger

from .admission_gate import SummaryAdmissionDeniedError, ensure_effect_admitted
from .dsh_summary_service import DshSummaryService
from .group_lock import GroupSummaryLockLostError, group_summary_lock_manager
from .history_service import (
    HistoryFetchMetadata,
    HistoryIncompleteError,
    check_group_history_supported,
)
from .image_renderer import render_summary_image_pages_base64
from .rendering import (
    format_partial_history_notice,
    format_time_range,
    summary_text_to_lines,
)
from .summary_task_data import (
    FrozenSummaryTaskConfig,
    SummaryTaskConfigError,
    freeze_summary_task_config,
)

if TYPE_CHECKING:
    from nonebot.adapters.onebot.v11 import Bot

    from .config_schema import DynamicConfigSchema
    from .summary_candidate import SummarySelection


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
    selection: SummarySelection | None
    image_base64: str
    image_pages_base64: tuple[str, ...]
    image_truncated: bool
    filter_label: str
    time_range: str
    history_fetch: HistoryFetchMetadata | None


SUMMARY_TITLE = "小鞠的总结时间到！"
EMPTY_HISTORY_TEXT = "可用的文本记录太少，没法总结……"

_group_lock_manager = group_summary_lock_manager
_dsh_summary_service = DshSummaryService()


def _empty_result(
    *,
    selection: SummarySelection | None = None,
) -> SummaryExecutionResult:
    return SummaryExecutionResult(
        summary_text="",
        filtered_message_count=0,
        selection=selection,
        image_base64="",
        image_pages_base64=(),
        image_truncated=False,
        filter_label="无",
        time_range="无",
        history_fetch=None,
    )


async def _execute_group_summary_core(
    *,
    bot: Bot,
    group_id: str,
    bot_self_id: str,
    user_request: str,
    config: FrozenSummaryTaskConfig,
    history_capability_confirmed: bool,
    request_trace_id: str,
) -> SummaryExecutionResult:
    """在已持有群租约时执行能力检查、DSH 总结和渲染。"""
    is_supported = history_capability_confirmed
    if not is_supported:
        is_supported = await check_group_history_supported(bot)
    if not is_supported:
        logger.error(
            "[GroupHistorySummary] 当前 Onebot/Napcat 实现尚未支持获取群聊记录能力"
        )
        raise CapabilityNotSupportedError

    logger.info("[GroupHistorySummary] 开始执行总结，trace_id={}", request_trace_id)

    generation = await _dsh_summary_service.generate(
        bot=bot,
        group_id=group_id,
        bot_self_id=bot_self_id,
        user_request=user_request,
        request_trace_id=request_trace_id,
        config=config,
    )
    selection = generation.selection
    history_fetch = selection.history_fetch if selection is not None else None
    partial_notice = format_partial_history_notice(history_fetch)

    if selection is None or not selection.messages:
        logger.info("[GroupHistorySummary] 没有可用的历史消息")
        empty_text = (
            f"{partial_notice}\n{EMPTY_HISTORY_TEXT}"
            if partial_notice
            else EMPTY_HISTORY_TEXT
        )
        return SummaryExecutionResult(
            summary_text=empty_text,
            filtered_message_count=0,
            selection=selection,
            image_base64="",
            image_pages_base64=(),
            image_truncated=False,
            filter_label="无",
            time_range="无",
            history_fetch=history_fetch,
        )

    result_summary_text = (
        f"{partial_notice}\n{generation.summary_text}"
        if partial_notice
        else generation.summary_text
    )
    body_lines = summary_text_to_lines(result_summary_text)
    filter_label = selection.source
    time_range = format_time_range(
        selection.messages[0].timestamp,
        selection.messages[-1].timestamp,
    )
    subtitle = f"{filter_label} {len(selection.messages)} 条 | {time_range}"
    # TSK-229：图片渲染为不可分瞬时效果；渲染前按目标群 BUSINESS 裁决，
    # 受限即抛 SummaryAdmissionDeniedError 终止本任务（正常控制流）。
    ensure_effect_admitted(group_id=group_id)
    image_render = await asyncio.to_thread(
        render_summary_image_pages_base64,
        title=SUMMARY_TITLE,
        subtitle=subtitle,
        body_lines=body_lines,
        layout_params=config.layout_params,
    )
    image_pages = image_render.images_base64

    return SummaryExecutionResult(
        summary_text=result_summary_text,
        filtered_message_count=len(selection.messages),
        selection=selection,
        image_base64=image_pages[0] if image_pages else "",
        image_pages_base64=image_pages,
        image_truncated=image_render.truncated,
        filter_label=filter_label,
        time_range=time_range,
        history_fetch=history_fetch,
    )


async def execute_group_summary(
    bot: Bot,
    group_id: str,
    bot_self_id: str,
    user_request: str,
    config: DynamicConfigSchema,
    requested_count: int | None = None,
    request_trace_id: str | None = None,
    *,
    history_capability_confirmed: bool = False,
) -> SummaryExecutionResult:
    """群聊历史总结的共享执行入口。

    由正常 handler 与 debug 插件共同调用，内部处理能力检查、群锁、DSH
    总结和图片渲染。正常入口已完成能力检查时，可通过
    ``history_capability_confirmed`` 避免对同一请求重复探测。
    ``request_trace_id`` 只用于普通运行日志；缺省时生成既有风格的请求 ID。

    Raises:
        SummaryBusyError: 同群已有总结任务运行中
        CapabilityNotSupportedError: 当前 OneBot 实现不支持群历史 API
        HistoryIncompleteError: 群历史读取失败且完整度低于配置阈值
        SummaryServiceUnavailableError: 分布式锁服务不可用或租约丢失
        SummaryTaskConfigError: 总结预算或历史限制无法冻结
    """
    trace_id = request_trace_id or f"group-summary-{uuid.uuid4().hex[:12]}"
    frozen = freeze_summary_task_config(config, requested_count=requested_count)
    try:
        lease = await _group_lock_manager.try_acquire(
            group_id=group_id,
            redis_db=frozen.redis_db,
            ttl_seconds=frozen.lock_ttl_seconds,
        )
    except Exception as exc:
        logger.error(
            "[GroupHistorySummary] 分布式锁获取失败: error_type={}",
            type(exc).__name__,
        )
        error = SummaryServiceUnavailableError("群总结服务暂时不可用，请稍后重试")
        raise error from None

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
                    config=frozen,
                    history_capability_confirmed=history_capability_confirmed,
                    request_trace_id=trace_id,
                )
            )
        except SummaryAdmissionDeniedError:
            # 准入拒绝为正常控制流：不记错误、不发正常失败通知/retry，瞬时
            # 任务终止且恢复后不复活；仍释放群锁（由 finally 统一收口）。
            return _empty_result()
        except GroupSummaryLockLostError:
            error = SummaryServiceUnavailableError(
                "群总结任务租约已失效，请稍后重试"
            )
            raise error from None
    finally:
        await lease.close()


__all__ = [
    "CapabilityNotSupportedError",
    "HistoryIncompleteError",
    "SummaryBusyError",
    "SummaryExecutionResult",
    "SummaryServiceUnavailableError",
    "SummaryTaskConfigError",
    "execute_group_summary",
]
