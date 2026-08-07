"""对话记忆无损分块测试。"""

import json

import pytest

from komari_bot.llm.content_budget import estimate_text_tokens
from komari_bot.plugins.komari_memory.services.message_chunking import (
    MessageChunkBudgetError,
    build_chunk_manifest,
    build_memory_external_context,
    chunk_messages_for_memory_processing,
)
from komari_bot.plugins.komari_memory.services.redis_manager import MessageSchema


def _message(
    index: int,
    *,
    content: str,
    nickname: str | None = None,
) -> MessageSchema:
    return MessageSchema(
        user_id=f"user-{index:04d}",
        user_nickname=nickname or f"用户{index:04d}",
        group_id="group-1",
        content=content,
        timestamp=float(index),
        message_id=f"message-{index:04d}",
    )


def test_two_thousand_messages_have_stable_complete_coverage() -> None:
    messages = [
        _message(
            index,
            content=(
                "十二千边界后的金丝雀🙂[CQ:at,qq=all]"
                if index == 1_999
                else f"第{index:04d}条消息"
            ),
        )
        for index in range(2_000)
    ]

    chunks = chunk_messages_for_memory_processing(
        messages,
        snapshot_fingerprint="snapshot-stable",
        bot_nickname="小鞠知花",
    )
    repeated = chunk_messages_for_memory_processing(
        list(messages),
        snapshot_fingerprint="snapshot-stable",
        bot_nickname="小鞠知花",
    )

    assert len(chunks) > 1
    assert [chunk.chunk_id for chunk in chunks] == [chunk.chunk_id for chunk in repeated]
    coverage = [item for chunk in chunks for item in chunk.coverage]
    assert [item.original_index for item in coverage] == list(range(2_000))
    assert all(item.fragment_count == 1 for item in coverage)
    assert [message.content for chunk in chunks for message in chunk.messages] == [
        message.content for message in messages
    ]
    assert "十二千边界后的金丝雀" in chunks[-1].messages[-1].content
    assert all(chunk.context_utf8_bytes <= 18_000 for chunk in chunks)
    assert all(chunk.estimated_tokens <= 6_000 for chunk in chunks)

    manifest_text = json.dumps(
        build_chunk_manifest(
            snapshot_fingerprint="snapshot-stable",
            chunks=chunks,
        ),
        ensure_ascii=False,
    )
    assert "十二千边界后的金丝雀" not in manifest_text
    assert "message-1999" in manifest_text


def test_oversized_unicode_and_cq_message_is_split_without_loss() -> None:
    original_content = "中文🙂[CQ:at,qq=all]&<>" * 300
    original = _message(7, content=original_content)

    chunks = chunk_messages_for_memory_processing(
        [original],
        snapshot_fingerprint="oversized",
        bot_nickname="小鞠知花",
        max_utf8_bytes=420,
        max_estimated_tokens=140,
    )

    fragments = [message for chunk in chunks for message in chunk.messages]
    coverage = [item for chunk in chunks for item in chunk.coverage]
    reconstructed = "".join(fragment.content.partition("\n")[2] for fragment in fragments)
    assert reconstructed == original_content
    assert len(fragments) > 1
    assert all(fragment.message_id == original.message_id for fragment in fragments)
    assert [item.fragment_index for item in coverage] == list(range(1, len(fragments) + 1))
    assert all(item.fragment_count == len(fragments) for item in coverage)
    assert all(item.original_chars == len(original_content) for item in coverage)
    for chunk in chunks:
        context, _, _ = build_memory_external_context(
            chunk.messages,
            bot_nickname="小鞠知花",
        )
        assert len(context.encode("utf-8")) <= 420
        assert estimate_text_tokens(context) <= 140


def test_fixed_message_metadata_over_budget_fails_instead_of_truncating() -> None:
    message = _message(1, content="正文", nickname="昵称" * 200)

    with pytest.raises(MessageChunkBudgetError, match="固定字段超出分块预算"):
        chunk_messages_for_memory_processing(
            [message],
            snapshot_fingerprint="too-small",
            bot_nickname="小鞠知花",
            max_utf8_bytes=80,
            max_estimated_tokens=30,
        )
