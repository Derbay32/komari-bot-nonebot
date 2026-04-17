"""群聊历史总结服务（仅提取总结正文）。"""

from __future__ import annotations

import re

from nonebot.plugin import require

from .history_service import HistoryMessage, format_message_for_prompt
from .prompt_template import get_template

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


async def summarize_history_messages(
    history_messages: list[HistoryMessage],
    model: str,
    temperature: float,
    max_tokens: int,
) -> str:
    """总结历史消息，返回总结正文。"""
    if not history_messages:
        return DEFAULT_SUMMARY_TEXT

    template = get_template()
    transcript = _build_transcript(history_messages)

    messages = [
        {
            "role": "system",
            "content": template["system_prompt"],
        },
        {
            "role": "user",
            "content": (f"<history_messages>\n{transcript}\n</history_messages>"),
        },
        {
            "role": template.get("memory_ack_role", "assistant"),
            "content": template["memory_ack"],
        },
        {
            "role": "system",
            "content": template["output_instruction"],
        },
        {
            "role": template.get("cot_prefix_role", "assistant"),
            "content": template["cot_prefix"],
        },
    ]

    raw_result = await llm_provider.generate_text_with_messages(
        messages=messages,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    summary_text = _extract_tag_content(raw_result, "content")

    if not summary_text:
        return DEFAULT_SUMMARY_TEXT

    return summary_text


def summary_text_to_lines(summary_text: str) -> list[str]:
    """将总结正文转换为图片渲染行。"""
    lines = [line.strip() for line in summary_text.splitlines()]
    normalized = [line for line in lines if line]
    return normalized or [DEFAULT_SUMMARY_TEXT]
