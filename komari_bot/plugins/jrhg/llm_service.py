"""JRHG LLM 调用服务。"""

from __future__ import annotations

import re
from typing import Any

from nonebot import logger
from nonebot.plugin import require

llm_provider = require("llm_provider")


def _summarize_prompt_messages(messages: list[dict[str, Any]]) -> dict[str, int]:
    """统计消息列表中的文本体量，便于追踪请求规模。"""
    text_parts = 0
    text_chars = 0

    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            text_parts += 1
            text_chars += len(content)

    return {
        "turns": len(messages),
        "text_parts": text_parts,
        "text_chars": text_chars,
    }


def _extract_tag_content(text: str, tag: str) -> str:
    """从 LLM 回复中提取指定 XML 标签内的内容。"""
    pattern = rf"<{tag}>([\s\S]*)</{tag}>"
    match = re.search(pattern, text)
    if match:
        return match.group(1).strip()

    logger.warning("[JRHG] 未找到 <{}> 标签，使用原始回复", tag)
    return re.sub(r"<think>[\s\S]*?</think>", "", text).strip()


async def generate_reply(
    *,
    messages: list[dict[str, Any]],
    request_trace_id: str | None = None,
) -> str:
    """调用 LLM 生成 JRHG 回复。"""
    config = llm_provider.config_manager.get()
    payload_stats = _summarize_prompt_messages(messages)
    logger.info(
        "[JRHG] LLM 请求追踪: trace_id={} turns={} text_parts={} text_chars={}",
        request_trace_id or "-",
        payload_stats["turns"],
        payload_stats["text_parts"],
        payload_stats["text_chars"],
    )

    raw_response = await llm_provider.generate_text_with_messages(
        messages=messages,
        model=config.deepseek_model,
        temperature=config.deepseek_temperature,
        max_tokens=config.deepseek_max_tokens,
        request_trace_id=request_trace_id,
    )
    return _extract_tag_content(raw_response, "content")
