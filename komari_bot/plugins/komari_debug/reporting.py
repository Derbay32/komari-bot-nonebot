"""诊断报告格式化与私密投递。

安全约束：
- 绝不输出完整历史、画像、搜索正文、prompt、reasoning content、base64
- 未报告的 token 字段显示为"未报告"而非 0；值为 0 时正确显示 0
- 零调用聚合全部显示"未报告"，不伪造 0
- 单节点按行切分并限制体积，每批不超过 50 个节点
- 合并转发失败时按同章节普通文本逐条发送
- 完整报告只投递到发起 SUPERUSER 的私聊
- 显式公开时仅发送二次脱敏的结构化诊断
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, MessageSegment

from komari_bot.common.onebot_messages import plain_text_message

if TYPE_CHECKING:
    from collections.abc import Mapping

    from komari_bot.plugins.llm_provider.diagnostic import (
        LLMCallTrace,
        LLMDiagnosticCollector,
        ToolExecutionTrace,
    )

MAX_NODE_TEXT_LENGTH = 3500
MAX_NODES_PER_BATCH = 50
_ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True, slots=True)
class DiagnosticDeliveryResult:
    """完整私聊报告与可选公开脱敏报告的投递结果。"""

    private_delivered: bool
    public_delivered: bool | None


def _fmt_token(val: int | None, *, complete: bool) -> str:
    if val is None or not complete:
        return "未报告"
    return str(val)


def _fmt_ms(val: float | None) -> str:
    if val is None:
        return "未报告"
    return f"{val:.0f}ms"


def _fmt_token_per_call(val: int | None) -> str:
    """格式化单次调用的 token 值：None → 未报告，0 → 0。"""
    if val is None:
        return "未报告"
    return str(val)


def _format_call_line(call: LLMCallTrace, index: int) -> str:
    """格式化单次 LLM 调用行。"""
    parts = [
        f"调用 #{index}: {call.phase}",
        f"  模型: {call.model or '未知'}",
        f"  轮次: {call.round_index}",
        f"  finish: {call.finish_reason or '未知'}",
        f"  耗时: {_fmt_ms(call.duration_ms)}",
    ]
    if call.parent_call_id:
        parts.append(f"  父调用: {call.parent_call_id}")
    if call.usage:
        u = call.usage
        parts.append(
            f"  token: in={_fmt_token_per_call(u.input_tokens)} "
            f"cache_hit={_fmt_token_per_call(u.cached_input_tokens)} "
            f"cache_miss={_fmt_token_per_call(u.cache_miss_input_tokens)} "
            f"out={_fmt_token_per_call(u.output_tokens)} "
            f"reasoning_out={_fmt_token_per_call(u.reasoning_output_tokens)} "
            f"total={_fmt_token_per_call(u.total_tokens)}"
        )
    else:
        parts.append("  token: 全部未报告")
    return "\n".join(parts)


def _format_tool_line(
    tool: ToolExecutionTrace,
    *,
    public_redacted: bool = False,
) -> str:
    """格式化单次工具执行行。"""
    lines = [
        f"工具: {tool.tool_name}",
        f"  状态: {tool.status}",
    ]
    if public_redacted:
        lines.append("  参数与结果: 公开模式已隐藏")
    else:
        safe_arguments = _build_safe_tool_arguments(
            tool.tool_name,
            tool.parsed_arguments,
        )
        lines.append(f"  参数: {_truncate_dict(safe_arguments, 200)}")
    if tool.error_summary and not public_redacted:
        lines.append("  错误: 已记录（异常正文已隐藏）")
    if tool.result_summary:
        lines.append("  结果: 已记录（内容已隐藏）")
    return "\n".join(lines)


def _build_safe_tool_arguments(
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """按工具白名单生成私聊诊断可展示的参数摘要。"""
    safe: dict[str, Any] = {}
    match tool_name:
        case "record_favorability_delta":
            delta = arguments.get("delta")
            if isinstance(delta, int):
                safe["delta"] = delta
        case "read_image":
            image_index = arguments.get("image_index")
            if isinstance(image_index, int):
                safe["image_index"] = image_index
        case "search_web":
            query = arguments.get("query")
            if isinstance(query, str):
                safe["query_chars"] = len(query)
        case "read_profile":
            keys = arguments.get("keys")
            if isinstance(keys, list):
                safe["requested_key_count"] = len(keys)
        case "final_response":
            content_chars = arguments.get("content_chars")
            if not isinstance(content_chars, int):
                content = arguments.get("content")
                content_chars = len(content) if isinstance(content, str) else None
            if content_chars is not None:
                safe["content_chars"] = content_chars
            has_history = arguments.get("has_interaction_history")
            if isinstance(has_history, bool):
                safe["has_interaction_history"] = has_history
            elif "interaction_history" in arguments:
                safe["has_interaction_history"] = isinstance(
                    arguments["interaction_history"],
                    dict,
                )
        case "fetch_recent_group_messages":
            safe = _pick_safe_scalar_arguments(
                arguments,
                "count",
                "include_bot_replies",
            )
        case "fetch_messages_by_user":
            safe = _pick_safe_scalar_arguments(arguments, "count", "scan_limit")
            safe["has_user_filter"] = bool(
                arguments.get("user_id") or arguments.get("display_name")
            )
        case "fetch_messages_by_topic":
            safe = _pick_safe_scalar_arguments(arguments, "count", "scan_limit")
            keywords = arguments.get("keywords")
            if isinstance(keywords, list):
                safe["keyword_count"] = len(keywords)
        case _:
            pass
    return safe


def _pick_safe_scalar_arguments(
    arguments: dict[str, Any],
    *keys: str,
) -> dict[str, int | bool]:
    """提取允许公开的整数或布尔参数。"""
    return {
        key: value
        for key in keys
        if isinstance((value := arguments.get(key)), (int, bool))
    }


def _truncate_dict(d: dict[str, Any], max_len: int) -> str:
    text = str(d)
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _truncate_text(content: str, max_chars: int = 500) -> str:
    """截断文本，用于错误信息、工具结果、参数和降级文本。"""
    if len(content) <= max_chars:
        return content
    return content[:max_chars] + "..."


def _safe_error_code(value: str | None) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if _ERROR_CODE_PATTERN.fullmatch(candidate) else "internal_error"


def _format_phase_subtotal(phase: str, collector: LLMDiagnosticCollector) -> str:
    """格式化阶段 token 小计，0 次调用时全部显示为未报告。"""
    agg = collector.aggregate_phase(phase)
    call_count = agg.call_count
    lines = [
        f"阶段 [{phase}] 小计 ({call_count} 次调用):",
        f"  输入: {_fmt_token(agg.input_tokens, complete=agg.input_tokens_complete and call_count > 0)}",
        f"  缓存命中: {_fmt_token(agg.cached_input_tokens, complete=agg.cached_input_tokens_complete and call_count > 0)}",
        f"  缓存未命中: {_fmt_token(agg.cache_miss_input_tokens, complete=agg.cache_miss_input_tokens_complete and call_count > 0)}",
        f"  输出: {_fmt_token(agg.output_tokens, complete=agg.output_tokens_complete and call_count > 0)}",
        f"  推理输出: {_fmt_token(agg.reasoning_output_tokens, complete=agg.reasoning_output_tokens_complete and call_count > 0)}",
        f"  总计: {_fmt_token(agg.total_tokens, complete=agg.total_tokens_complete and call_count > 0)}",
    ]
    return "\n".join(lines)


def _format_overall_total(collector: LLMDiagnosticCollector) -> str:
    """格式化全链路 token 总计，0 次调用时全部显示为未报告。"""
    agg = collector.aggregate_overall()
    call_count = agg.call_count
    lines = [
        f"全链路总计 ({call_count} 次调用):",
        f"  输入: {_fmt_token(agg.input_tokens, complete=agg.input_tokens_complete and call_count > 0)}",
        f"  缓存命中: {_fmt_token(agg.cached_input_tokens, complete=agg.cached_input_tokens_complete and call_count > 0)}",
        f"  缓存未命中: {_fmt_token(agg.cache_miss_input_tokens, complete=agg.cache_miss_input_tokens_complete and call_count > 0)}",
        f"  输出: {_fmt_token(agg.output_tokens, complete=agg.output_tokens_complete and call_count > 0)}",
        f"  推理输出: {_fmt_token(agg.reasoning_output_tokens, complete=agg.reasoning_output_tokens_complete and call_count > 0)}",
        f"  总计: {_fmt_token(agg.total_tokens, complete=agg.total_tokens_complete and call_count > 0)}",
    ]
    return "\n".join(lines)


def _split_into_nodes(text: str) -> list[str]:
    """将长文本拆分为合并转发节点的内容列表（按行切分，限制单节点体积）。"""
    nodes: list[str] = []
    current_lines: list[str] = []
    current_len = 0

    for line in text.splitlines(keepends=True):
        line_len = len(line)

        # 如果当前行本身超过限制，先与已有缓存合并，再单独切分超长行
        if line_len > MAX_NODE_TEXT_LENGTH:
            if current_lines:
                nodes.append("".join(current_lines))
                current_lines = []
                current_len = 0
            # 将超长行切分为多个块
            nodes.extend(
                line[offset : offset + MAX_NODE_TEXT_LENGTH]
                for offset in range(0, line_len, MAX_NODE_TEXT_LENGTH)
            )
            continue

        if current_len + line_len > MAX_NODE_TEXT_LENGTH and current_lines:
            nodes.append("".join(current_lines))
            current_lines = []
            current_len = 0
        current_lines.append(line)
        current_len += line_len

    if current_lines:
        content = "".join(current_lines)
        if current_len > MAX_NODE_TEXT_LENGTH:
            content = content[:MAX_NODE_TEXT_LENGTH]
        nodes.append(content)

    if not nodes:
        nodes.append("(无诊断信息)")

    return nodes


def _build_chapters(
    collector: LLMDiagnosticCollector,
    result_type: str,
    *,
    succeeded: bool,
    error: str | None,
    extra_info: Mapping[str, Any] | None,
    final_result_info: Mapping[str, Any] | None = None,
    public_redacted: bool = False,
) -> list[tuple[str, str]]:
    """构建报告章节列表，每项为 (章节标题, 章节正文)。

    extra_info: 安全的请求元数据（user_id, content 等），不放回复正文
    final_result_info: 最终结果信息（reply_text, favorability_delta 等），
                       进入"最终结果"章节且回复正文不截断
    """
    chapters: list[tuple[str, str]] = []

    # 1. 请求总览：仅安全元数据
    overview_lines = [
        f"请求 ID: {collector.request_id}",
        f"类型: {result_type}",
        f"状态: {'成功' if succeeded else '失败'}",
    ]
    if extra_info and not public_redacted:
        for key, val in extra_info.items():
            safe_val = _truncate_text(str(val), max_chars=300)
            overview_lines.append(f"{key}: {safe_val}")
    chapters.append(("请求总览", "\n".join(overview_lines)))

    # 2. 最终结果
    if public_redacted:
        final_lines = [
            f"最终结果: {'已成功完成' if succeeded else '执行失败'}",
            "输入、输出、用户标识与异常正文已隐藏",
        ]
    elif succeeded:
        final_lines = ["最终结果: 已成功完成"]
        if final_result_info:
            for key, val in final_result_info.items():
                if key == "reply_text":
                    final_lines.append(f"回复正文:\n{val}")
                elif key == "favorability_delta":
                    final_lines.append(f"拟议好感度: {val}")
                elif key == "reply_to_message_id":
                    final_lines.append(f"引用消息 ID: {val}")
                else:
                    safe_val = _truncate_text(str(val), max_chars=500)
                    final_lines.append(f"{key}: {safe_val}")
    else:
        final_lines = [
            "最终结果: 执行失败",
            f"错误码: {_safe_error_code(error)}",
        ]
        if final_result_info:
            for key, val in final_result_info.items():
                safe_val = _truncate_text(str(val), max_chars=300)
                final_lines.append(f"{key}: {safe_val}")
    chapters.append(("最终结果", "\n".join(final_lines)))

    # 3. LLM 调用详情（按阶段/轮次分组）
    if collector.calls:
        call_lines: list[str] = []
        ordered_calls = sorted(
            collector.calls,
            key=lambda c: (c.phase, c.round_index, c.call_id),
        )
        current_phase = ""
        for idx, call in enumerate(ordered_calls):
            if call.phase != current_phase:
                current_phase = call.phase
                call_lines.append(f"\n--- 阶段: {current_phase} ---")
            call_lines.append(_format_call_line(call, idx + 1))
        chapters.append(("LLM 调用详情", "\n".join(call_lines)))
    else:
        chapters.append(("LLM 调用详情", "无 LLM 调用记录"))

    # 4. 工具摘要
    if collector.tools:
        tool_lines: list[str] = []
        grouped: dict[str, list] = {}
        for t in collector.tools:
            grouped.setdefault(t.call_id, []).append(t)
        for call_id, tools in grouped.items():
            tool_lines.append(f"\n调用 {call_id} 的工具:")
            tool_lines.extend(
                _format_tool_line(t, public_redacted=public_redacted) for t in tools
            )
        chapters.append(("工具摘要", "\n".join(tool_lines)))
    else:
        chapters.append(("工具摘要", "无工具调用记录"))

    # 5. 阶段 token 小计
    if collector.calls:
        phases = sorted({c.phase for c in collector.calls})
        subtotal_lines = [
            _format_phase_subtotal(phase, collector) for phase in phases
        ]
        chapters.append(("阶段 token 小计", "\n".join(subtotal_lines)))
    else:
        chapters.append(("阶段 token 小计", "无"))

    # 6. 全链路 token 小计
    chapters.append(("全链路 token 小计", _format_overall_total(collector)))

    # 7. 错误/降级
    if collector.errors:
        if public_redacted:
            error_lines = [
                f"共 {len(collector.errors)} 个错误或降级事件，详情已隐藏"
            ]
        else:
            error_lines = [
                f"阶段: {err['phase']} | "
                f"类型: {_truncate_text(err['type'], max_chars=100)} | "
                "异常正文: 已隐藏"
                for err in collector.errors
            ]
        chapters.append(("错误/降级", "\n".join(error_lines)))
    else:
        chapters.append(("错误/降级", "无"))

    return chapters


async def build_and_send_diagnostic_report(
    *,
    bot: Bot,
    user_id: int,
    collector: LLMDiagnosticCollector,
    result_type: str,
    succeeded: bool,
    error: str | None = None,
    extra_info: Mapping[str, Any] | None = None,
    final_result_info: Mapping[str, Any] | None = None,
    public_group_id: int | None = None,
) -> DiagnosticDeliveryResult:
    """私聊完整报告，并可向群内额外投递二次脱敏报告。

    Args:
        bot: OneBot Bot 实例
        user_id: 已通过鉴权的 SUPERUSER ID
        collector: 诊断收集器
        result_type: "reply" 或 "summary"
        succeeded: 是否成功
        error: 错误信息（失败时）
        extra_info: 私聊报告使用的请求元数据
        final_result_info: 最终结果信息（回复正文、好感度变化等）
        public_group_id: 显式请求公开脱敏报告时的目标群 ID
    """
    private_chapters = _build_chapters(
        collector,
        result_type,
        succeeded=succeeded,
        error=error,
        extra_info=extra_info,
        final_result_info=final_result_info,
    )
    private_delivered = await _send_report_nodes(
        bot,
        _chapters_to_nodes(private_chapters),
        forward_api="send_private_forward_msg",
        text_api="send_private_msg",
        target_key="user_id",
        target_id=user_id,
        channel="private",
    )

    public_delivered: bool | None = None
    if public_group_id is not None:
        public_chapters = _build_chapters(
            collector,
            result_type,
            succeeded=succeeded,
            error=error,
            extra_info=extra_info,
            final_result_info=final_result_info,
            public_redacted=True,
        )
        public_delivered = await _send_report_nodes(
            bot,
            _chapters_to_nodes(public_chapters),
            forward_api="send_group_forward_msg",
            text_api="send_group_msg",
            target_key="group_id",
            target_id=public_group_id,
            channel="public_redacted",
        )

    return DiagnosticDeliveryResult(
        private_delivered=private_delivered,
        public_delivered=public_delivered,
    )


def _chapters_to_nodes(chapters: list[tuple[str, str]]) -> list[str]:
    """把报告章节转换为受大小限制的合并转发节点。"""
    nodes: list[str] = []
    for title, body in chapters:
        chapter_text = f"【{title}】\n{body}"
        nodes.extend(_split_into_nodes(chapter_text))
    return nodes


async def _send_report_nodes(
    bot: Bot,
    nodes: list[str],
    *,
    forward_api: str,
    text_api: str,
    target_key: str,
    target_id: int,
    channel: str,
) -> bool:
    """发送报告节点；合并转发失败时仅向同一目标降级为普通消息。"""
    delivered = True
    for batch_idx in range(0, len(nodes), MAX_NODES_PER_BATCH):
        batch = nodes[batch_idx : batch_idx + MAX_NODES_PER_BATCH]
        forward_nodes = [
            MessageSegment.node_custom(
                user_id=int(bot.self_id),
                nickname="Komari Debug",
                content=plain_text_message(node_text),
            )
            for node_text in batch
        ]

        try:
            await bot.call_api(
                forward_api,
                **{target_key: target_id},
                messages=forward_nodes,
            )
            logger.info(
                "[KomariDebug] 诊断报告发送成功: channel={} batch={}/{} nodes={}",
                channel,
                batch_idx // MAX_NODES_PER_BATCH + 1,
                (len(nodes) - 1) // MAX_NODES_PER_BATCH + 1,
                len(batch),
            )
        except Exception as exc:
            logger.warning(
                "[KomariDebug] 合并转发失败，降级为同目标普通消息: "
                "channel={} batch={} error_type={}",
                channel,
                batch_idx // MAX_NODES_PER_BATCH + 1,
                type(exc).__name__,
            )
            batch_delivered = await _send_text_fallback(
                bot,
                batch,
                text_api=text_api,
                target_key=target_key,
                target_id=target_id,
                channel=channel,
            )
            delivered = delivered and batch_delivered
    return delivered


async def _send_text_fallback(
    bot: Bot,
    batch_nodes: list[str],
    *,
    text_api: str,
    target_key: str,
    target_id: int,
    channel: str,
) -> bool:
    """合并转发失败时，将节点逐条发回同一个私聊或群聊目标。"""
    delivered = True
    for node_text in batch_nodes:
        node_delivered = await _send_message(
            bot,
            api=text_api,
            target_key=target_key,
            target_id=target_id,
            message=node_text,
            channel=channel,
        )
        delivered = delivered and node_delivered
    return delivered


async def _send_message(
    bot: Bot,
    *,
    api: str,
    target_key: str,
    target_id: int,
    message: Any,
    channel: str,
) -> bool:
    """向指定目标发送消息，日志不记录目标 ID 或消息正文。"""
    safe_message = plain_text_message(message) if isinstance(message, str) else message
    try:
        await bot.call_api(api, **{target_key: target_id}, message=safe_message)
    except Exception as exc:
        logger.warning(
            "[KomariDebug] 消息投递失败: channel={} error_type={}",
            channel,
            type(exc).__name__,
        )
        return False
    return True


async def send_private_message(bot: Bot, user_id: int, message: Any) -> bool:
    """向已鉴权的 SUPERUSER 私聊发送文本或消息段。"""
    return await _send_message(
        bot,
        api="send_private_msg",
        target_key="user_id",
        target_id=user_id,
        message=message,
        channel="private",
    )


async def send_group_text(bot: Bot, group_id: int, text: str) -> bool:
    """发送不含诊断正文的群消息文本。"""
    return await _send_message(
        bot,
        api="send_group_msg",
        target_key="group_id",
        target_id=group_id,
        message=plain_text_message(text),
        channel="group_receipt",
    )
