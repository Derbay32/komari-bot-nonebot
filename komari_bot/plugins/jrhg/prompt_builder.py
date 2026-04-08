"""JRHG 提示词构建器。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .prompt_template import get_template

EMPTY_INTERACTION_HISTORY: dict[str, Any] = {
    "file_type": "用户的近期对鞠行为备忘录",
    "description": "暂无互动记录",
    "records": [],
    "summary": "",
}


def build_prompt(
    *,
    daily_favor: int,
    interaction_history: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """构建 JRHG 的 OpenAI messages 提示词。"""
    template = get_template()
    request_text = template["request_text"].strip()
    history_payload = (
        interaction_history
        if isinstance(interaction_history, dict)
        else EMPTY_INTERACTION_HISTORY
    )
    interaction_history_text = json.dumps(history_payload, ensure_ascii=False)

    system_parts = [
        template["system_prompt"],
        f"<current_time>{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}</current_time>",
        f"<favorability>{daily_favor}</favorability>",
    ]

    return [
        {"role": "system", "content": "\n\n".join(system_parts)},
        {
            "role": "user",
            "content": (
                f"<interaction_history>{interaction_history_text}</interaction_history>\n"
                f"<request_text>{request_text}</request_text>"
            ),
        },
        {"role": "assistant", "content": template["memory_ack"]},
        {"role": "system", "content": template["output_instruction"]},
        {"role": "assistant", "content": template["cot_prefix"]},
    ]
