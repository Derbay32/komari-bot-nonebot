"""对话记忆任务的无损消息分块。"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import TYPE_CHECKING

from komari_bot.llm.content_budget import estimate_text_tokens

if TYPE_CHECKING:
    from .redis_manager import MessageSchema


MEMORY_CHUNK_MAX_UTF8_BYTES = 18_000
MEMORY_CHUNK_MAX_ESTIMATED_TOKENS = 6_000
MEMORY_UNTRUSTED_CONTEXT_MAX_CHARS = MEMORY_CHUNK_MAX_UTF8_BYTES

_FRAGMENT_BUDGET_MARKER = (
    "[消息分片 99999999999999999999/99999999999999999999]\n"
)


class MessageChunkBudgetError(ValueError):
    """单条消息的固定元数据已超出分块预算。"""

    def __init__(self, message_id: object, *, fragment_marker: bool = False) -> None:
        detail = "分片标记" if fragment_marker else "固定字段"
        super().__init__(f"消息 {message_id!r} 的{detail}超出分块预算")


class InvalidMessageChunkBudgetError(ValueError):
    """消息分块预算不是正整数。"""

    def __init__(self) -> None:
        super().__init__("消息分块预算必须大于 0")


@dataclass(frozen=True, slots=True)
class MessageFragmentCoverage:
    """一个处理片段对应的原始消息范围。"""

    original_index: int
    message_id: str
    original_chars: int
    original_utf8_bytes: int
    fragment_index: int
    fragment_count: int

    @property
    def fragmented(self) -> bool:
        return self.fragment_count > 1

    def to_dict(self) -> dict[str, int | str | bool]:
        """生成可持久化的覆盖记录。"""
        return {
            "original_index": self.original_index,
            "message_id": self.message_id,
            "original_chars": self.original_chars,
            "original_utf8_bytes": self.original_utf8_bytes,
            "fragment_index": self.fragment_index,
            "fragment_count": self.fragment_count,
            "fragmented": self.fragmented,
        }


@dataclass(frozen=True, slots=True)
class MessageProcessingChunk:
    """一组可在单次 LLM 请求中完整处理的消息片段。"""

    chunk_id: str
    index: int
    messages: tuple[MessageSchema, ...]
    coverage: tuple[MessageFragmentCoverage, ...]
    context_utf8_bytes: int
    estimated_tokens: int

    def to_manifest_entry(self) -> dict[str, object]:
        """生成不包含正文的持久化清单条目。"""
        return {
            "chunk_id": self.chunk_id,
            "index": self.index,
            "context_utf8_bytes": self.context_utf8_bytes,
            "estimated_tokens": self.estimated_tokens,
            "coverage": [item.to_dict() for item in self.coverage],
        }


@dataclass(frozen=True, slots=True)
class _PendingFragment:
    message: MessageSchema
    coverage: MessageFragmentCoverage


def format_message_line(message: MessageSchema, *, bot_nickname: str) -> str:
    """按总结与画像 Agent 的共同格式渲染一条消息。"""
    if message.is_bot:
        return f"[bot] {bot_nickname}: {message.content}"
    return f"[user_id:{message.user_id}] {message.user_nickname}: {message.content}"


def collect_chunk_participants(
    messages: tuple[MessageSchema, ...] | list[MessageSchema],
) -> tuple[list[str], dict[str, str]]:
    """按首次出现顺序收集参与用户和昵称。"""
    participants: list[str] = []
    display_name_map: dict[str, str] = {}
    seen: set[str] = set()
    for message in messages:
        if message.is_bot:
            continue
        user_id = str(message.user_id)
        if user_id not in seen:
            seen.add(user_id)
            participants.append(user_id)
        nickname = str(message.user_nickname).strip()
        if nickname and user_id not in display_name_map:
            display_name_map[user_id] = nickname
    return participants, display_name_map


def build_memory_external_context(
    messages: tuple[MessageSchema, ...] | list[MessageSchema],
    *,
    bot_nickname: str,
) -> tuple[str, list[str], dict[str, str]]:
    """构建分块预算实际覆盖的三段式外部上下文。"""
    participants, display_name_map = collect_chunk_participants(messages)
    conversation_text = "\n".join(
        format_message_line(message, bot_nickname=bot_nickname) for message in messages
    )
    external_context = (
        f"【群聊记录】\n{conversation_text}\n\n"
        f"【参与用户 user_id】\n{json.dumps(participants, ensure_ascii=False)}\n\n"
        f"【昵称映射】\n{json.dumps(display_name_map, ensure_ascii=False)}"
    )
    return external_context, participants, display_name_map


def chunk_messages_for_memory_processing(
    messages: list[MessageSchema],
    *,
    snapshot_fingerprint: str,
    bot_nickname: str,
    max_utf8_bytes: int = MEMORY_CHUNK_MAX_UTF8_BYTES,
    max_estimated_tokens: int = MEMORY_CHUNK_MAX_ESTIMATED_TOKENS,
) -> list[MessageProcessingChunk]:
    """按消息边界和双预算无损分块，超长单条消息显式切片。"""
    if max_utf8_bytes < 1 or max_estimated_tokens < 1:
        raise InvalidMessageChunkBudgetError
    if not messages:
        return []

    pending_fragments: list[_PendingFragment] = []
    for original_index, message in enumerate(messages):
        pending_fragments.extend(
            _split_message_if_needed(
                message,
                original_index=original_index,
                bot_nickname=bot_nickname,
                max_utf8_bytes=max_utf8_bytes,
                max_estimated_tokens=max_estimated_tokens,
            )
        )

    grouped: list[list[_PendingFragment]] = []
    current: list[_PendingFragment] = []
    for fragment in pending_fragments:
        candidate = [*current, fragment]
        if _within_budget(
            [item.message for item in candidate],
            bot_nickname=bot_nickname,
            max_utf8_bytes=max_utf8_bytes,
            max_estimated_tokens=max_estimated_tokens,
        ):
            current = candidate
            continue
        if not current:
            raise MessageChunkBudgetError(fragment.coverage.message_id)
        grouped.append(current)
        current = [fragment]
    if current:
        grouped.append(current)

    chunks: list[MessageProcessingChunk] = []
    for chunk_index, group in enumerate(grouped):
        chunk_messages = tuple(item.message for item in group)
        coverage = tuple(item.coverage for item in group)
        context, _, _ = build_memory_external_context(
            chunk_messages,
            bot_nickname=bot_nickname,
        )
        chunks.append(
            MessageProcessingChunk(
                chunk_id=_build_chunk_id(
                    snapshot_fingerprint=snapshot_fingerprint,
                    chunk_index=chunk_index,
                    coverage=coverage,
                ),
                index=chunk_index,
                messages=chunk_messages,
                coverage=coverage,
                context_utf8_bytes=len(context.encode("utf-8")),
                estimated_tokens=estimate_text_tokens(context),
            )
        )
    return chunks


def build_chunk_manifest(
    *,
    snapshot_fingerprint: str,
    chunks: list[MessageProcessingChunk],
) -> dict[str, object]:
    """生成不含消息正文的稳定覆盖账本。"""
    return {
        "version": 1,
        "snapshot_fingerprint": snapshot_fingerprint,
        "chunk_count": len(chunks),
        "chunks": [chunk.to_manifest_entry() for chunk in chunks],
    }


def _split_message_if_needed(
    message: MessageSchema,
    *,
    original_index: int,
    bot_nickname: str,
    max_utf8_bytes: int,
    max_estimated_tokens: int,
) -> list[_PendingFragment]:
    original_content = str(message.content)
    original_utf8_bytes = len(original_content.encode("utf-8"))
    whole_coverage = MessageFragmentCoverage(
        original_index=original_index,
        message_id=str(message.message_id),
        original_chars=len(original_content),
        original_utf8_bytes=original_utf8_bytes,
        fragment_index=1,
        fragment_count=1,
    )
    if _within_budget(
        [message],
        bot_nickname=bot_nickname,
        max_utf8_bytes=max_utf8_bytes,
        max_estimated_tokens=max_estimated_tokens,
    ):
        return [_PendingFragment(message=message, coverage=whole_coverage)]

    pieces = _split_content(
        message,
        content=original_content,
        bot_nickname=bot_nickname,
        max_utf8_bytes=max_utf8_bytes,
        max_estimated_tokens=max_estimated_tokens,
    )
    fragment_count = len(pieces)
    fragments: list[_PendingFragment] = []
    for fragment_index, piece in enumerate(pieces, start=1):
        fragment_message = replace(
            message,
            content=f"[消息分片 {fragment_index}/{fragment_count}]\n{piece}",
        )
        if not _within_budget(
            [fragment_message],
            bot_nickname=bot_nickname,
            max_utf8_bytes=max_utf8_bytes,
            max_estimated_tokens=max_estimated_tokens,
        ):
            raise MessageChunkBudgetError(message.message_id, fragment_marker=True)
        fragments.append(
            _PendingFragment(
                message=fragment_message,
                coverage=MessageFragmentCoverage(
                    original_index=original_index,
                    message_id=str(message.message_id),
                    original_chars=len(original_content),
                    original_utf8_bytes=original_utf8_bytes,
                    fragment_index=fragment_index,
                    fragment_count=fragment_count,
                ),
            )
        )
    return fragments


def _split_content(
    message: MessageSchema,
    *,
    content: str,
    bot_nickname: str,
    max_utf8_bytes: int,
    max_estimated_tokens: int,
) -> list[str]:
    if not content:
        raise MessageChunkBudgetError(message.message_id)

    pieces: list[str] = []
    cursor = 0
    while cursor < len(content):
        low = 1
        high = len(content) - cursor
        best = 0
        while low <= high:
            middle = (low + high) // 2
            candidate = replace(
                message,
                content=f"{_FRAGMENT_BUDGET_MARKER}{content[cursor : cursor + middle]}",
            )
            if _within_budget(
                [candidate],
                bot_nickname=bot_nickname,
                max_utf8_bytes=max_utf8_bytes,
                max_estimated_tokens=max_estimated_tokens,
            ):
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        if best < 1:
            raise MessageChunkBudgetError(message.message_id)
        pieces.append(content[cursor : cursor + best])
        cursor += best
    return pieces


def _within_budget(
    messages: list[MessageSchema],
    *,
    bot_nickname: str,
    max_utf8_bytes: int,
    max_estimated_tokens: int,
) -> bool:
    context, _, _ = build_memory_external_context(
        messages,
        bot_nickname=bot_nickname,
    )
    return (
        len(context.encode("utf-8")) <= max_utf8_bytes
        and estimate_text_tokens(context) <= max_estimated_tokens
    )


def _build_chunk_id(
    *,
    snapshot_fingerprint: str,
    chunk_index: int,
    coverage: tuple[MessageFragmentCoverage, ...],
) -> str:
    payload = {
        "snapshot_fingerprint": snapshot_fingerprint,
        "chunk_index": chunk_index,
        "coverage": [item.to_dict() for item in coverage],
    }
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(raw.encode("utf-8")).hexdigest()
