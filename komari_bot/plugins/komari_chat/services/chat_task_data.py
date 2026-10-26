"""Closed, bounded data envelope supplied to the DSH chat preset.

The model never receives Python-built messages, system prompts, tool schemas,
image URLs, callback names, addresses, credentials, or bank identities. This
module builds only the separate untrusted/fact sections defined by TSK-203.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, cast

from komari_bot.llm.content_budget import (
    TextBudget,
    truncate_text_to_budget,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from komari_bot.context.redis_store import MessageSchema
    from komari_bot.plugins.komari_chat.services.reply_context import ReplyContext

UntrustedSourceType = Literal[
    "conversation_history",
    "quoted_message",
    "memory",
    "knowledge",
]
TrustLevel = Literal["untrusted", "low"]
SourceSide = Literal["assistant", "user"]


class FavorabilityProfile(Protocol):
    """Structural read surface of the trusted current-favorability fact."""

    favorability: int
    stage_index: int
    stage_name: str
    stage_prompt: str


MAX_RECENT_MESSAGES = 10
MAX_UNTRUSTED_CONTENT_CHARS = 4_000
_UNTRUSTED_CONTENT_BUDGET = TextBudget(
    max_characters=MAX_UNTRUSTED_CONTENT_CHARS,
    max_utf8_bytes=MAX_UNTRUSTED_CONTENT_CHARS * 3,
    max_estimated_tokens=MAX_UNTRUSTED_CONTENT_CHARS,
)
_REQUIRED_CURRENT_SOURCE: UntrustedSourceType = "conversation_history"
_REQUIRED_QUOTE_SOURCE: UntrustedSourceType = "quoted_message"


@dataclass(frozen=True, slots=True)
class UntrustedData:
    source_type: UntrustedSourceType
    source_id: str
    trust_level: TrustLevel
    content: str

    def to_dict(self) -> dict[str, object]:
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "trust_level": self.trust_level,
            "content": self.content,
        }


@dataclass(frozen=True, slots=True)
class MessageData(UntrustedData):
    message_id: str
    group_id: str
    user_id: str
    user_nickname: str
    timestamp: float
    is_bot: bool

    def to_dict(self) -> dict[str, object]:
        return {
            **UntrustedData.to_dict(self),
            "message_id": self.message_id,
            "group_id": self.group_id,
            "user_id": self.user_id,
            "user_nickname": self.user_nickname,
            "timestamp": self.timestamp,
            "is_bot": self.is_bot,
        }


@dataclass(frozen=True, slots=True)
class QuotedData(UntrustedData):
    message_id: str
    user_id: str | None
    user_nickname: str | None
    source_side: SourceSide
    image_count: int
    has_visible_image: bool

    def to_dict(self) -> dict[str, object]:
        return {
            **UntrustedData.to_dict(self),
            "message_id": self.message_id,
            "user_id": self.user_id,
            "user_nickname": self.user_nickname,
            "source_side": self.source_side,
            "image_count": self.image_count,
            "has_visible_image": self.has_visible_image,
        }


@dataclass(frozen=True, slots=True)
class CurrentFavorabilityFact:
    value: int
    stage_index: int
    stage_name: str
    stage_prompt: str

    def to_dict(self) -> dict[str, object]:
        return {
            "value": self.value,
            "stage_index": self.stage_index,
            "stage_name": self.stage_name,
            "stage_prompt": self.stage_prompt,
        }


@dataclass(frozen=True, slots=True)
class VisibleUserFact:
    user_id: str
    nickname: str
    character_name: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "user_id": self.user_id,
            "nickname": self.nickname,
            "character_name": self.character_name,
        }


@dataclass(frozen=True, slots=True)
class ChatFacts:
    bot_nickname: str
    current_favorability: CurrentFavorabilityFact
    visible_users: tuple[VisibleUserFact, ...]
    max_favorability_delta: int

    def to_dict(self) -> dict[str, object]:
        return {
            "bot_nickname": self.bot_nickname,
            "current_favorability": self.current_favorability.to_dict(),
            "visible_users": [user.to_dict() for user in self.visible_users],
            "max_favorability_delta": self.max_favorability_delta,
        }


@dataclass(frozen=True, slots=True)
class ChatTaskData:
    """The one closed JSON payload consumed by the DSH chat input decoder."""

    schema: int
    current: MessageData
    recent: tuple[MessageData, ...]
    quote: QuotedData | None
    memory: tuple[UntrustedData, ...]
    knowledge: tuple[UntrustedData, ...]
    uid_knowledge: tuple[UntrustedData, ...]
    facts: ChatFacts

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "current": self.current.to_dict(),
            "recent": [message.to_dict() for message in self.recent],
            "quote": None if self.quote is None else self.quote.to_dict(),
            "memory": [item.to_dict() for item in self.memory],
            "knowledge": [item.to_dict() for item in self.knowledge],
            "uid_knowledge": [item.to_dict() for item in self.uid_knowledge],
            "facts": self.facts.to_dict(),
        }


def bounded_untrusted_content(value: object) -> str:
    """Apply the same per-item extraction bound used by the old context path."""
    text = str(value)
    if not text:
        return ""
    bounded, _truncated = truncate_text_to_budget(
        text,
        label="不可信上下文正文",
        budget=_UNTRUSTED_CONTENT_BUDGET,
    )
    return bounded


def build_chat_task_data(
    *,
    current: MessageSchema,
    recent: Sequence[MessageSchema],
    quote: ReplyContext | None,
    memory_results: Sequence[object],
    knowledge_results: Sequence[object],
    uid_knowledge_results: Sequence[tuple[str, object]],
    favorability: object,
    bot_nickname: str,
    max_favorability_delta: int,
    group_bindings: Mapping[str, str],
) -> ChatTaskData:
    """Build the exact task data from actual source fields without guessing IDs."""
    same_group_recent = [
        message
        for message in recent
        if message.group_id == current.group_id
        and message.message_id != current.message_id
    ][-MAX_RECENT_MESSAGES:]

    return ChatTaskData(
        schema=1,
        current=_message_data(current, trust_level="untrusted"),
        recent=tuple(
            _message_data(message, trust_level="untrusted")
            for message in same_group_recent
        ),
        quote=None if quote is None else _quoted_data(quote),
        memory=tuple(_memory_data(result, index) for index, result in enumerate(memory_results)),
        knowledge=tuple(
            _knowledge_data(result, index)
            for index, result in enumerate(knowledge_results)
        ),
        uid_knowledge=tuple(
            _uid_knowledge_data(uid, result, index)
            for index, (uid, result) in enumerate(uid_knowledge_results)
        ),
        facts=_facts(
            current=current,
            recent=same_group_recent,
            quote=quote,
            favorability=favorability,
            bot_nickname=bot_nickname,
            max_favorability_delta=max_favorability_delta,
            group_bindings=group_bindings,
        ),
    )


def _message_data(message: MessageSchema, *, trust_level: TrustLevel) -> MessageData:
    return MessageData(
        source_type=_REQUIRED_CURRENT_SOURCE,
        source_id=message.message_id,
        trust_level=trust_level,
        content=message.content,
        message_id=message.message_id,
        group_id=message.group_id,
        user_id=message.user_id,
        user_nickname=message.user_nickname,
        timestamp=float(message.timestamp),
        is_bot=bool(message.is_bot),
    )


def _quoted_data(quote: ReplyContext) -> QuotedData:
    return QuotedData(
        source_type=_REQUIRED_QUOTE_SOURCE,
        source_id=quote.message_id,
        trust_level="untrusted",
        content=quote.text,
        message_id=quote.message_id,
        user_id=quote.user_id,
        user_nickname=quote.user_nickname,
        source_side=quote.source_side,
        image_count=int(quote.image_count),
        has_visible_image=bool(quote.has_visible_image),
    )


def _memory_data(result: object, index: int) -> UntrustedData:
    identifier = getattr(result, "id", None)
    source_id = (
        str(identifier)
        if identifier is not None and str(identifier)
        else f"memory:index:{index}"
    )
    return UntrustedData(
        source_type="memory",
        source_id=source_id,
        trust_level="untrusted",
        content=bounded_untrusted_content(getattr(result, "text", "")),
    )


def _knowledge_data(result: object, index: int) -> UntrustedData:
    return UntrustedData(
        source_type="knowledge",
        source_id=_knowledge_source_id(result, index),
        trust_level="low",
        content=bounded_untrusted_content(getattr(result, "content", "")),
    )


def _uid_knowledge_data(uid: str, result: object, index: int) -> UntrustedData:
    identifier = getattr(result, "id", None)
    source_id = (
        f"uid:{uid}:{identifier}"
        if identifier is not None and str(identifier)
        else f"knowledge:uid:{uid}:index:{index}"
    )
    return UntrustedData(
        source_type="knowledge",
        source_id=source_id,
        trust_level="low",
        content=bounded_untrusted_content(getattr(result, "content", "")),
    )


def _knowledge_source_id(result: object, index: int) -> str:
    identifier = getattr(result, "id", None)
    if identifier is not None and str(identifier):
        return str(identifier)
    return f"knowledge:index:{index}"


def _facts(
    *,
    current: MessageSchema,
    recent: Sequence[MessageSchema],
    quote: ReplyContext | None,
    favorability: object,
    bot_nickname: str,
    max_favorability_delta: int,
    group_bindings: Mapping[str, str],
) -> ChatFacts:
    visible: list[VisibleUserFact] = []
    seen: set[str] = set()

    def add(user_id: str, nickname: str | None) -> None:
        if user_id in seen:
            return
        seen.add(user_id)
        character_name = group_bindings.get(user_id)
        visible.append(
            VisibleUserFact(
                user_id=user_id,
                nickname=nickname or user_id,
                character_name=character_name or None,
            )
        )

    for message in recent:
        if not message.is_bot:
            add(message.user_id, message.user_nickname)
    add(current.user_id, current.user_nickname)
    if quote is not None and quote.source_side == "user" and quote.user_id:
        add(quote.user_id, quote.user_nickname)

    profile = cast("FavorabilityProfile", favorability)
    return ChatFacts(
        bot_nickname=bot_nickname,
        current_favorability=CurrentFavorabilityFact(
            value=int(profile.favorability),
            stage_index=int(profile.stage_index),
            stage_name=str(profile.stage_name),
            stage_prompt=str(profile.stage_prompt),
        ),
        visible_users=tuple(visible),
        max_favorability_delta=max_favorability_delta,
    )


__all__ = [
    "MAX_RECENT_MESSAGES",
    "ChatFacts",
    "ChatTaskData",
    "CurrentFavorabilityFact",
    "FavorabilityProfile",
    "MessageData",
    "QuotedData",
    "UntrustedData",
    "VisibleUserFact",
    "bounded_untrusted_content",
    "build_chat_task_data",
]
