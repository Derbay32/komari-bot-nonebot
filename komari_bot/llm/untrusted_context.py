"""LLM 不可信上下文的结构化边界。"""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Literal

from .content_budget import (
    TextBudget,
    truncate_text_to_budget,
)

type UntrustedSourceType = Literal[
    "knowledge",
    "web",
    "group_history",
    "quoted_message",
    "conversation_history",
    "memory",
    "profile",
    "vision",
    "tool_result",
]
type TrustLevel = Literal["untrusted", "low"]

_DEFAULT_MAX_CONTEXT_CHARS = 12_000
_MAX_SOURCE_ID_CHARS = 256
_MAX_CONTEXT_UTF8_BYTES = 36_000
_INVALID_MAX_CONTEXT_CHARS_ERROR = "max_chars 必须大于 0"


@dataclass(frozen=True, slots=True)
class UntrustedContext:
    """一段带来源和信任级别的不可信正文。"""

    source_type: UntrustedSourceType
    source_id: str
    content: str
    trust_level: TrustLevel = "untrusted"
    max_chars: int = _DEFAULT_MAX_CONTEXT_CHARS


def render_untrusted_context(
    context: UntrustedContext,
    *,
    max_chars: int | None = None,
) -> str:
    """渲染不可闭合标签边界的外部数据块。"""
    max_chars = context.max_chars if max_chars is None else max_chars
    if max_chars < 1:
        raise ValueError(_INVALID_MAX_CONTEXT_CHARS_ERROR)

    original_chars = len(context.content)
    content = context.content[:max_chars]

    source_type = html.escape(context.source_type, quote=True)
    escaped_source_id = html.escape(
        context.source_id[:_MAX_SOURCE_ID_CHARS],
        quote=True,
    )
    source_id, source_id_truncated = truncate_text_to_budget(
        escaped_source_id,
        label="不可信上下文来源 ID",
        budget=TextBudget(
            max_characters=_MAX_SOURCE_ID_CHARS,
            max_utf8_bytes=_MAX_SOURCE_ID_CHARS * 3,
            max_estimated_tokens=_MAX_SOURCE_ID_CHARS,
        ),
    )
    trust_level = html.escape(context.trust_level, quote=True)
    escaped_content, escaped_truncated = truncate_text_to_budget(
        html.escape(content, quote=False),
        label="不可信上下文正文",
        budget=TextBudget(
            max_characters=max_chars,
            max_utf8_bytes=min(_MAX_CONTEXT_UTF8_BYTES, max_chars * 3),
            max_estimated_tokens=max_chars,
        ),
    )
    truncated = (
        original_chars > max_chars
        or escaped_truncated
        or source_id_truncated
    )
    return (
        f'<untrusted_context source_type="{source_type}" '
        f'source_id="{source_id}" trust_level="{trust_level}" '
        f'original_chars="{original_chars}" truncated="{str(truncated).lower()}">\n'
        f"<data>{escaped_content}</data>\n"
        "</untrusted_context>"
    )
