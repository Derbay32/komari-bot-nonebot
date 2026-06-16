"""跨群互动事件 LLM 总结服务。"""

from __future__ import annotations

import json
import re
from typing import Any

from nonebot import logger
from nonebot.plugin import require
from pydantic import BaseModel, Field

from ..services.config_interface import get_config

llm_provider = require("llm_provider")


class InteractionEventSummaryResult(BaseModel):
    """跨群互动事件总结结果。"""

    event_summary: str
    importance: int = Field(default=4, ge=1, le=5)


def _extract_json_object(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned)
    if match:
        cleaned = match.group(1).strip()
    else:
        object_match = re.search(r"\{[\s\S]*\}", cleaned)
        if object_match:
            cleaned = object_match.group(0)
    parsed = json.loads(cleaned)
    return parsed if isinstance(parsed, dict) else {}


def _format_records(records: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for index, record in enumerate(records, start=1):
        event = str(record.get("event", "")).strip()
        result = str(record.get("result", "")).strip()
        emotion = str(record.get("emotion", "")).strip()
        lines.append(
            f"{index}. 用户行为：{event}\n"
            f"   小鞠反应：{result}\n"
            f"   小鞠感受：{emotion}"
        )
    return "\n".join(lines)


async def summarize_interaction_events(
    *,
    user_id: str,
    display_name: str,
    records: list[dict[str, Any]],
) -> InteractionEventSummaryResult:
    """将一批原始互动记录聚合为一条长期互动事件记忆。"""
    config = get_config()
    prompt = (
        "你是小鞠知花的长期记忆整理器。请基于下面一批跨群互动原始记录，"
        "用小鞠知花的主观视角总结成一条可长期保存的互动事件记忆。\n"
        "要求：\n"
        "1. 只输出 JSON，不要 Markdown，不要额外解释。\n"
        '2. JSON 格式必须是 {"event_summary": "...", "importance": 1-5}。\n'
        "3. 不要逐条流水账，要提炼用户偏好、互动模式、关系变化和重要事件。\n"
        "4. 不得输出群号、群名，也不要暗示来自哪个群。\n"
        "5. event_summary 使用简体中文，保留小鞠对用户的关系感受和可复用偏好信息。\n\n"
        f"用户 ID：{user_id}\n"
        f"用户显示名：{display_name}\n"
        f"原始互动记录：\n{_format_records(records)}"
    )
    response = await llm_provider.generate_text(
        prompt=prompt,
        model=config.llm_model_summary,
        temperature=config.llm_temperature_summary,
        max_tokens=min(config.llm_max_tokens_summary, 800),
        thinking_mode=config.llm_thinking_mode_summary,
        reasoning_effort=config.llm_reasoning_effort_summary,
        request_trace_id=f"interaction-event-summary-{user_id}",
        request_phase="interaction_event_summary",
    )
    try:
        payload = _extract_json_object(response)
        result = InteractionEventSummaryResult.model_validate(payload)
    except Exception:
        logger.exception("[KomariMemory] 互动事件总结 JSON 解析失败，使用降级总结: user={}", user_id)
        fallback = "；".join(
            str(record.get("event", "")).strip()
            for record in records[:5]
            if str(record.get("event", "")).strip()
        )
        result = InteractionEventSummaryResult(
            event_summary=fallback or f"{display_name} 与小鞠发生了一批值得后续参考的互动。",
            importance=4,
        )

    result.event_summary = re.sub(r"\s+", " ", result.event_summary).strip()
    if not result.event_summary:
        result.event_summary = f"{display_name} 与小鞠发生了一批值得后续参考的互动。"
    return result
