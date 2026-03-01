"""群聊历史总结服务。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from nonebot.plugin import require

from .history_service import HistoryMessage, format_message_for_prompt

llm_provider = require("llm_provider")


@dataclass(slots=True)
class SummaryResult:
    """总结结果。"""

    title: str
    overview: str
    highlights: list[str]
    todos: list[str]
    risks: list[str]


def _trim_code_fence(raw_text: str) -> str:
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_json_result(raw_text: str) -> SummaryResult:
    stripped = _trim_code_fence(raw_text)
    data = json.loads(stripped)

    title = str(data.get("title", "群聊总结")).strip() or "群聊总结"
    overview = str(data.get("overview", "")).strip()

    def to_text_list(value: object) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    highlights = to_text_list(data.get("highlights"))
    todos = to_text_list(data.get("todos"))
    risks = to_text_list(data.get("risks"))

    return SummaryResult(
        title=title,
        overview=overview,
        highlights=highlights,
        todos=todos,
        risks=risks,
    )


def _build_transcript(history_messages: list[HistoryMessage], max_chars: int = 12000) -> str:
    lines: list[str] = []
    total_chars = 0

    for message in history_messages:
        line = format_message_for_prompt(message)
        if len(line) > 220:
            line = f"{line[:220]}..."
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
) -> SummaryResult:
    """总结历史消息。"""
    transcript = _build_transcript(history_messages)

    messages = [
        {
            "role": "system",
            "content": (
                "你是中文群聊纪要助手。"
                "请从聊天事实中提炼信息，不要臆造。"
                "输出必须是 JSON 对象，不要额外文本。"
            ),
        },
        {
            "role": "user",
            "content": (
                "请总结下面的群聊记录，并输出 JSON，结构如下：\n"
                '{\n'
                '  "title": "不超过18字的标题",\n'
                '  "overview": "80-180字总体概览",\n'
                '  "highlights": ["关键点1", "关键点2"],\n'
                '  "todos": ["待办1", "待办2"],\n'
                '  "risks": ["风险1", "风险2"]\n'
                "}\n"
                "要求：\n"
                "1. highlights/todos/risks 各 0-5 条。\n"
                "2. 只基于记录中明确出现的信息。\n"
                "3. 没有对应内容时返回空数组。\n\n"
                f"聊天记录：\n{transcript}"
            ),
        },
    ]

    raw_result = await llm_provider.generate_text_with_messages(
        messages=messages,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
    )

    try:
        return _parse_json_result(raw_result)
    except Exception:
        return SummaryResult(
            title="群聊总结",
            overview=_trim_code_fence(raw_result),
            highlights=[],
            todos=[],
            risks=[],
        )


def summary_result_to_lines(summary: SummaryResult) -> list[str]:
    """将总结结果转换为图片渲染文本行。"""
    lines: list[str] = []

    if summary.overview:
        lines.append("概览")
        lines.append(summary.overview)
        lines.append("")

    lines.append("关键点")
    if summary.highlights:
        lines.extend(f"- {item}" for item in summary.highlights)
    else:
        lines.append("- 无")
    lines.append("")

    lines.append("待办事项")
    if summary.todos:
        lines.extend(f"- {item}" for item in summary.todos)
    else:
        lines.append("- 无")
    lines.append("")

    lines.append("风险与关注")
    if summary.risks:
        lines.extend(f"- {item}" for item in summary.risks)
    else:
        lines.append("- 无")

    return lines
