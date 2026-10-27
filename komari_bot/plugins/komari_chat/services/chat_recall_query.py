"""Bounded automatic Hindsight recall-query composition for DSH chat tasks.

The trigger is highest priority. Explicit quote identity stays visible even
when the quote body must be reduced, and history is removed oldest-first until
the complete query fits the shared ``QUERY_TEXT_BUDGET``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from komari_bot.llm.content_budget import (
    QUERY_TEXT_BUDGET,
    ContentValidationError,
    validate_text_budget,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from komari_bot.context.redis_store import MessageSchema
    from komari_bot.plugins.komari_chat.services.reply_context import ReplyContext

MEDIA_ONLY_RECALL_QUERY = "当前交流"
MAX_RECALL_HISTORY_MESSAGES = 10


class RecallQueryBudgetError(RuntimeError):
    """The mandatory current trigger plus quote identity cannot fit the budget."""

    code = "recall_query_unfit"


@dataclass(frozen=True, slots=True)
class _Block:
    rendered: str
    history: bool = False


def build_automatic_recall_query(
    *,
    current: MessageSchema,
    recent: Sequence[MessageSchema],
    quote: ReplyContext | None,
) -> str:
    """Compose exactly one automatic recall query from chat Redis context."""
    trigger_text = current.content.strip() or MEDIA_ONLY_RECALL_QUERY
    current_block = _render_current(current, trigger_text)
    _require_bounded([current_block], label="召回查询")

    indexed_group = [
        (index, message)
        for index, message in enumerate(recent)
        if message.group_id == current.group_id
    ]
    trigger_positions = [
        index
        for index, message in indexed_group
        if message.message_id == current.message_id
    ]
    trigger_position = trigger_positions[0] if trigger_positions else None
    eligible = [
        (index, message)
        for index, message in indexed_group
        if message.message_id != current.message_id
        and (trigger_position is None or index < trigger_position)
        and message.timestamp <= current.timestamp
    ]
    eligible.sort(key=lambda item: (item[1].timestamp, item[0]))
    history = [
        message for _index, message in eligible[-MAX_RECALL_HISTORY_MESSAGES:]
    ]

    quote_text = None if quote is None else quote.text.strip()
    quote_full = (
        _render_quote(quote, quote_text)
        if quote is not None and quote_text
        else None
    )
    quote_compact = None if quote is None else _render_quote_identity(quote)

    rendered_history = [
        _render_history(message, str(index))
        for index, message in enumerate(history)
    ]

    # First build the richest candidate. Remove the oldest history entries
    # before reducing quote detail. The trigger and the explicit quote identity
    # are mandatory and are never silently discarded for budget pressure.
    quote_choice = quote_full if quote_full is not None else quote_compact
    parts = [current_block]
    if quote_choice is not None:
        parts.append(quote_choice)
    parts.extend(rendered_history)
    while _is_over_budget(parts) and len(rendered_history) > 0:
        rendered_history.pop(0)
        parts = [current_block]
        if quote_choice is not None:
            parts.append(quote_choice)
        parts.extend(rendered_history)

    if _is_over_budget(parts) and quote_full is not None:
        quote_choice = quote_compact
        parts = [current_block]
        if quote_choice is not None:
            parts.append(quote_choice)
        parts.extend(rendered_history)

    if _is_over_budget(parts):
        # The mandatory trigger plus the compact explicit quote identity does
        # not fit. Never drop the quote relationship or truncate the trigger.
        raise RecallQueryBudgetError
    _require_bounded(parts, label="召回查询")
    return "\n".join(parts)


def _render_current(current: MessageSchema, trigger_text: str) -> str:
    attrs = _attrs(
        message_id=current.message_id,
        user_id=current.user_id,
        user_nickname=current.user_nickname,
        is_bot=current.is_bot,
    )
    return f"[current_message {attrs}]\n{trigger_text}\n[/current_message]"


def _render_quote_identity(quote: ReplyContext) -> str:
    attrs = _attrs(
        message_id=quote.message_id,
        user_id=quote.user_id,
        source_side=quote.source_side,
        image_count=quote.image_count,
        has_visible_image=quote.has_visible_image,
    )
    return f"[quote {attrs}][/quote]"


def _render_quote(quote: ReplyContext, quote_text: str) -> str:
    attrs = _attrs(
        message_id=quote.message_id,
        user_id=quote.user_id,
        source_side=quote.source_side,
        image_count=quote.image_count,
        has_visible_image=quote.has_visible_image,
    )
    return f"[quote {attrs}]\n{quote_text}\n[/quote]"


def _render_history(message: MessageSchema, index: str) -> str:
    attrs = _attrs(
        message_id=message.message_id,
        user_id=message.user_id,
        user_nickname=message.user_nickname,
        is_bot=message.is_bot,
        index=index,
    )
    return f"[history {attrs}]\n{message.content}\n[/history]"


def _attrs(**values: object) -> str:
    return " ".join(
        f"{name}={json.dumps(value, ensure_ascii=False)}"
        for name, value in values.items()
    )


def _is_over_budget(parts: Sequence[str]) -> bool:
    try:
        _require_bounded(parts, label="召回查询")
    except RecallQueryBudgetError:
        return True
    return False


def _require_bounded(parts: Sequence[str], *, label: str) -> None:
    query = "\n".join(parts)
    try:
        validate_text_budget(query, label=label, budget=QUERY_TEXT_BUDGET)
    except ContentValidationError:
        raise RecallQueryBudgetError from None


__all__ = [
    "MAX_RECALL_HISTORY_MESSAGES",
    "MEDIA_ONLY_RECALL_QUERY",
    "RecallQueryBudgetError",
    "build_automatic_recall_query",
]
