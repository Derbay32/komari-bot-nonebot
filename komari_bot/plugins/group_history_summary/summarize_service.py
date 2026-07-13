"""群聊历史总结服务（仅提取总结正文）。"""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING

from nonebot.plugin import require

from komari_bot.common.dsv4_instruct import inject_dsv4_instruct_to_first_user_message

from .history_service import HistoryMessage, format_message_for_prompt
from .prompt_template import get_template

if TYPE_CHECKING:
    from komari_bot.plugins.llm_provider.base_client import LLMCompletionResultSchema
    from komari_bot.plugins.llm_provider.diagnostic import LLMDiagnosticCollector

llm_provider = require("llm_provider")

DEFAULT_SUMMARY_TEXT = "本次聊天记录信息较少，暂无可提炼的有效总结。"


def _extract_tag_content(text: str, tag: str) -> str:
    """提取指定 XML 标签内容。"""
    pattern = rf"<{tag}>([\s\S]*)</{tag}>"
    match = re.search(pattern, text)
    if match:
        return match.group(1).strip()

    without_think = re.sub(r"<think>[\s\S]*?</think>", "", text)
    return without_think.strip()


def _build_transcript(
    history_messages: list[HistoryMessage], max_chars: int = 12000
) -> str:
    lines: list[str] = []
    total_chars = 0

    for message in history_messages:
        line = format_message_for_prompt(message)
        if len(line) > 240:
            line = f"{line[:240]}..."
        if total_chars + len(line) > max_chars:
            break
        lines.append(line)
        total_chars += len(line)

    return "\n".join(lines)


async def _summarize_history_internal(
    history_messages: list[HistoryMessage],
    model: str,
    temperature: float,
    max_tokens: int,
    *,
    assistant_prefill_enabled: bool = False,
    dsv4_roleplay_instruct_mode: str = "auto",
    thinking_mode: bool = False,
    reasoning_effort: str = "",
    request_trace_id: str | None = None,
    collector: "LLMDiagnosticCollector | None" = None,
) -> tuple[str, "LLMCompletionResultSchema | None"]:
    """内部总结方法，同时返回文本和 completion 详情。

    当 collector 非 None 时使用 generate_messages_completion 获取
    completion 级元数据并记录到 collector；否则使用 str 便捷接口。
    """
    if not history_messages:
        return DEFAULT_SUMMARY_TEXT, None

    template = get_template()
    transcript = _build_transcript(history_messages)

    messages: list[dict[str, object]] = [
        {"role": "user", "content": template["system_prompt"]},
        {"role": "user", "content": template["output_instruction"]},
        {"role": "user", "content": f"<history_messages>\n{transcript}\n</history_messages>"},
    ]
    messages = inject_dsv4_instruct_to_first_user_message(
        messages,  # type: ignore[arg-type]
        model=model,
        mode=dsv4_roleplay_instruct_mode,
    )

    if assistant_prefill_enabled:
        messages.extend(
            [
                {"role": template.get("memory_ack_role", "assistant"), "content": template["memory_ack"]},
                {"role": template.get("cot_prefix_role", "assistant"), "content": template["cot_prefix"]},
            ]
        )

    if collector is not None:
        from komari_bot.plugins.llm_provider.diagnostic import LLMCallTrace

        completion = await llm_provider.generate_messages_completion(
            messages=messages,  # type: ignore[arg-type]
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            thinking_mode=thinking_mode,
            reasoning_effort=reasoning_effort,
            request_trace_id=request_trace_id,
            request_phase="group_history_summary_final",
        )

        collector.add_call(
            LLMCallTrace(
                call_id=uuid.uuid4().hex[:12],
                parent_call_id=request_trace_id,
                phase="group_history_summary_final",
                round_index=0,
                model=model,
                finish_reason=completion.finish_reason,
                duration_ms=completion.duration_ms,
                usage=completion.usage,
            )
        )

        summary_text = _extract_tag_content(completion.content, "content")
        if not summary_text:
            return DEFAULT_SUMMARY_TEXT, completion

        return summary_text, completion

    raw_result = await llm_provider.generate_text_with_messages(
        messages=messages,  # type: ignore[arg-type]
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        thinking_mode=thinking_mode,
        reasoning_effort=reasoning_effort,
        request_phase="group_history_summary",
    )
    summary_text = _extract_tag_content(raw_result, "content")

    if not summary_text:
        return DEFAULT_SUMMARY_TEXT, None

    return summary_text, None


async def summarize_history_messages(
    history_messages: list[HistoryMessage],
    model: str,
    temperature: float,
    max_tokens: int,
    *,
    assistant_prefill_enabled: bool = False,
    dsv4_roleplay_instruct_mode: str = "auto",
    thinking_mode: bool = False,
    reasoning_effort: str = "",
    request_trace_id: str | None = None,
    collector: "LLMDiagnosticCollector | None" = None,
) -> str:
    """总结历史消息，返回总结正文。"""
    text, _completion = await _summarize_history_internal(
        history_messages,
        model,
        temperature,
        max_tokens,
        assistant_prefill_enabled=assistant_prefill_enabled,
        dsv4_roleplay_instruct_mode=dsv4_roleplay_instruct_mode,
        thinking_mode=thinking_mode,
        reasoning_effort=reasoning_effort,
        request_trace_id=request_trace_id,
        collector=collector,
    )
    return text


def summary_text_to_lines(summary_text: str) -> list[str]:
    """将总结正文转换为图片渲染行。"""
    lines = [line.strip() for line in summary_text.splitlines()]
    normalized = [line for line in lines if line]
    return normalized or [DEFAULT_SUMMARY_TEXT]
