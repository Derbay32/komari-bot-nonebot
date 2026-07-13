"""诊断报告格式化与合并转发发送。

安全约束：
- 绝不输出完整历史、画像、搜索正文、prompt、reasoning content、base64
- 未报告的 token 字段显示为"未报告"而非 0；值为 0 时正确显示 0
- 零调用聚合全部显示"未报告"，不伪造 0
- 单节点按行切分并限制体积，每批不超过 50 个节点
- 合并转发失败时按同章节普通文本逐条发送
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, MessageSegment

if TYPE_CHECKING:
    from komari_bot.plugins.llm_provider.diagnostic import (
        LLMCallTrace,
        LLMDiagnosticCollector,
        ToolExecutionTrace,
    )

MAX_NODE_TEXT_LENGTH = 3500
MAX_NODES_PER_BATCH = 50


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


def _format_tool_line(tool: ToolExecutionTrace) -> str:
    """格式化单次工具执行行。"""
    safe_arguments = _build_safe_tool_arguments(
        tool.tool_name,
        tool.parsed_arguments,
    )
    lines = [
        f"工具: {tool.tool_name}",
        f"  状态: {tool.status}",
        f"  参数: {_truncate_dict(safe_arguments, 200)}",
    ]
    if tool.error_summary:
        lines.append(f"  错误: {_truncate_text(tool.error_summary, 300)}")
    if tool.result_summary:
        lines.append("  结果: 已记录（内容已隐藏）")
    return "\n".join(lines)


def _build_safe_tool_arguments(
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """按工具白名单生成可在群内展示的诊断参数。"""
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
    extra_info: dict[str, Any] | None,
    final_result_info: dict[str, Any] | None = None,
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
    if extra_info:
        for key, val in extra_info.items():
            safe_val = _truncate_text(str(val), max_chars=300)
            overview_lines.append(f"{key}: {safe_val}")
    chapters.append(("请求总览", "\n".join(overview_lines)))

    # 2. 最终结果
    if succeeded:
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
            f"错误类型: {_truncate_text(error or '未知', max_chars=300)}",
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
            tool_lines.extend(_format_tool_line(t) for t in tools)
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
        error_lines = [
            f"阶段: {err['phase']} | 类型: {_truncate_text(err['type'], max_chars=100)} | 消息: {_truncate_text(err['message'], max_chars=300)}"
            for err in collector.errors
        ]
        chapters.append(("错误/降级", "\n".join(error_lines)))
    else:
        chapters.append(("错误/降级", "无"))

    return chapters


async def build_and_send_diagnostic_report(
    *,
    bot: Bot,
    group_id: int,
    collector: LLMDiagnosticCollector,
    result_type: str,
    succeeded: bool,
    error: str | None = None,
    extra_info: dict[str, Any] | None = None,
    final_result_info: dict[str, Any] | None = None,
) -> None:
    """构建诊断报告并通过合并转发发送；失败时降级为普通文本逐条发送。

    Args:
        bot: OneBot Bot 实例
        group_id: 目标群 ID
        collector: 诊断收集器
        result_type: "reply" 或 "summary"
        succeeded: 是否成功
        error: 错误信息（失败时）
        extra_info: 安全的请求元数据（不放回复正文）
        final_result_info: 最终结果信息（回复正文、好感度变化等）
    """
    chapters = _build_chapters(
        collector,
        result_type,
        succeeded=succeeded,
        error=error,
        extra_info=extra_info,
        final_result_info=final_result_info,
    )

    # 为每个章节构建节点（章节标题作为第一行，正文随后）
    nodes: list[str] = []
    for title, body in chapters:
        chapter_text = f"【{title}】\n{body}"
        nodes.extend(_split_into_nodes(chapter_text))

    # 分批发送合并转发
    for batch_idx in range(0, len(nodes), MAX_NODES_PER_BATCH):
        batch = nodes[batch_idx : batch_idx + MAX_NODES_PER_BATCH]
        forward_nodes = [
            MessageSegment.node_custom(
                user_id=int(bot.self_id),
                nickname="Komari Debug",
                content=node_text,
            )
            for node_text in batch
        ]

        try:
            await bot.call_api(
                "send_group_forward_msg",
                group_id=group_id,
                messages=forward_nodes,
            )
            logger.info(
                "[KomariDebug] 合并转发报告发送成功: group={} batch={}/{} nodes={}",
                group_id,
                batch_idx // MAX_NODES_PER_BATCH + 1,
                (len(nodes) - 1) // MAX_NODES_PER_BATCH + 1,
                len(batch),
            )
        except Exception:
            logger.exception(
                "[KomariDebug] 合并转发失败，降级为普通文本发送: group={} batch={}",
                group_id,
                batch_idx // MAX_NODES_PER_BATCH + 1,
            )
            await _send_text_fallback(bot, group_id, batch)


async def _send_text_fallback(
    bot: Bot,
    group_id: int,
    batch_nodes: list[str],
) -> None:
    """合并转发失败时，将同一批次的节点逐条作为普通文本发送。"""
    for node_text in batch_nodes:
        try:
            await send_group_text(bot, group_id, node_text)
        except Exception:
            logger.exception("[KomariDebug] 文本降级发送失败")


async def send_group_text(bot: Bot, group_id: int, text: str) -> None:
    """发送普通群消息文本。"""
    try:
        await bot.call_api(
            "send_group_msg",
            group_id=group_id,
            message=text,
        )
    except Exception:
        logger.exception("[KomariDebug] send_group_msg 失败")
