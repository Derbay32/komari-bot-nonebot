"""TSK-206 总结候选、可信分页与完成回执。"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from komari_bot.agent.types import MAX_RESULT_BYTES
from komari_bot.plugins.group_history_summary.history_service import (
    HistoryFetchMetadata,
    HistoryMessage,
)
from komari_bot.plugins.group_history_summary.history_tools import SummaryToolResult
from komari_bot.plugins.group_history_summary.summary_candidate import (
    MAX_HISTORY_PAGE_BYTES,
    MAX_ROW_CONTENT_CHARS,
    SummaryCandidate,
    SummaryCompletedError,
    SummaryPageRangeError,
    SummaryPageTooLargeError,
    SummaryResultMismatchError,
    SummaryStaleGenerationError,
)

BOT_ID = "999"


def _message(
    seq: int,
    *,
    content: str | None = None,
    user_id: str = "1001",
    nickname: str = "阿明",
    reply_to: str | None = None,
) -> HistoryMessage:
    return HistoryMessage(
        user_id=user_id,
        nickname=nickname,
        content=f"消息{seq}" if content is None else content,
        timestamp=seq,
        message_seq=seq,
        message_id=f"m{seq}",
        reply_to_message_id=reply_to,
    )


def _tool_result(
    messages: list[HistoryMessage],
    *,
    source: str = "recent_group_messages",
    matched_count: int | None = None,
    metadata: HistoryFetchMetadata | None = None,
) -> SummaryToolResult:
    return SummaryToolResult(
        source=source,
        matched_count=len(messages) if matched_count is None else matched_count,
        messages=messages,
        filters={"count": 10},
        history_fetch=metadata,
    )


def _candidate_with(messages: list[HistoryMessage], **kwargs: Any) -> SummaryCandidate:
    candidate = SummaryCandidate(bot_self_id=BOT_ID)
    candidate.replace_selection(_tool_result(messages, **kwargs))
    return candidate


def test_generation_is_monotonic_and_stale_pages_are_rejected() -> None:
    candidate = _candidate_with([_message(1)])
    first = candidate.generation
    candidate.replace_selection(_tool_result([_message(2)]))

    assert candidate.generation == first + 1
    with pytest.raises(SummaryStaleGenerationError):
        candidate.page(first, 0)


def test_page_is_oldest_first_bounded_and_carries_identity() -> None:
    candidate = _candidate_with(
        [_message(seq, reply_to="m0" if seq == 1 else None) for seq in range(1, 5)]
    )

    page = candidate.page(candidate.generation, 0)

    assert page["total"] == 4
    assert page["next_offset"] is None
    rows = page["rows"]
    assert isinstance(rows, list)
    assert [row["message_id"] for row in rows] == ["m1", "m2", "m3", "m4"]
    first = rows[0]
    assert first["reply_to_message_id"] == "m0"
    assert first["user_id"] == "1001"
    assert first["nickname"] == "阿明"
    assert first["is_bot"] is False
    assert first["source_type"] == "group_history"
    assert first["source_id"] == "recent_group_messages"
    assert first["trust_level"] == "untrusted"


def test_page_caps_content_at_the_transcript_contribution_policy() -> None:
    candidate = _candidate_with([_message(1, content="x" * 500)])

    page = candidate.page(candidate.generation, 0)
    row = page["rows"][0]

    assert len(row["content"]) == MAX_ROW_CONTENT_CHARS


def test_large_selection_pages_across_the_bridge_boundary() -> None:
    candidate = _candidate_with([_message(seq) for seq in range(1, 151)])

    offsets: list[int] = []
    offset = 0
    while offset is not None:
        page = candidate.page(candidate.generation, offset)
        encoded = json.dumps(page, ensure_ascii=False, separators=(",", ":"))
        assert len(encoded.encode("utf-8")) <= MAX_HISTORY_PAGE_BYTES
        offsets.append(offset)
        offset = page["next_offset"]

    assert offsets == [0, 64, 128]
    assert len(offsets) > 1


def test_page_packs_by_serialized_byte_budget_before_the_row_cap() -> None:
    long_nickname = "名" * 6000
    candidate = _candidate_with(
        [_message(seq, nickname=long_nickname) for seq in range(1, 41)]
    )

    first = candidate.page(candidate.generation, 0)
    encoded = json.dumps(first, ensure_ascii=False, separators=(",", ":"))
    assert len(encoded.encode("utf-8")) <= MAX_HISTORY_PAGE_BYTES
    assert 0 < len(first["rows"]) < 40
    assert first["rows"][0]["nickname"] == long_nickname
    assert first["next_offset"] == len(first["rows"])

    second = candidate.page(candidate.generation, first["next_offset"])
    assert second["offset"] == first["next_offset"]
    assert second["rows"]


def test_out_of_range_offsets_are_rejected() -> None:
    candidate = _candidate_with([_message(1)])

    with pytest.raises(SummaryPageRangeError):
        candidate.page(candidate.generation, -1)
    with pytest.raises(SummaryPageRangeError):
        candidate.page(candidate.generation, 2)


def test_single_row_that_cannot_fit_the_page_envelope_fails_closed() -> None:
    candidate = _candidate_with([_message(1, nickname="n" * (MAX_HISTORY_PAGE_BYTES + 1))])

    with pytest.raises(SummaryPageTooLargeError):
        candidate.page(candidate.generation, 0)


def test_final_before_history_is_history_required() -> None:
    candidate = SummaryCandidate(bot_self_id=BOT_ID)

    receipt = candidate.submit_final({"content": "太早总结"})

    assert receipt.accepted is False
    assert receipt.code == "history_required"
    assert list(receipt.missing) == ["history_read", "final_summary"]
    assert receipt.result is None


def test_empty_successful_selection_still_requires_the_final_tool() -> None:
    candidate = _candidate_with([])

    assert candidate.selection is not None
    assert candidate.selection.messages == ()
    assert list(candidate.missing_requirements) == ["final_summary"]

    receipt = candidate.submit_final({"content": "空历史的总结"})
    assert receipt.accepted is True
    assert receipt.code is None
    assert receipt.missing == ()
    assert receipt.result == {"summary_text": "空历史的总结"}


def test_invalid_final_content_is_correctable_and_preserves_selection() -> None:
    candidate = _candidate_with([_message(1)])

    for payload in (
        {},
        {"content": ""},
        {"content": "   "},
        {"content": 1},
        {"content": "ok", "extra": True},
    ):
        receipt = candidate.submit_final(payload)
        assert receipt.accepted is False
        assert receipt.code == "invalid_content"
        assert list(receipt.missing) == ["final_summary"]
        assert receipt.result is None
        assert candidate.selection is not None

    accepted = candidate.submit_final({"content": " 修正后的总结 "})
    assert accepted.accepted is True
    assert accepted.result == {"summary_text": "修正后的总结"}


def test_final_content_must_fit_the_result_envelope() -> None:
    candidate = _candidate_with([_message(1)])
    oversized = "x" * (MAX_RESULT_BYTES + 1)

    receipt = candidate.submit_final({"content": oversized})

    assert receipt.accepted is False
    assert receipt.code == "invalid_content"


def test_completion_freezes_generation_and_rejects_further_effects() -> None:
    candidate = _candidate_with([_message(1)])
    receipt = candidate.submit_final({"content": "完成"})
    assert receipt.accepted is True

    again = candidate.submit_final({"content": "再来一次"})
    assert again.accepted is False
    assert again.code == "already_completed"
    assert again.result == {"summary_text": "完成"}

    with pytest.raises(SummaryCompletedError):
        candidate.replace_selection(_tool_result([_message(2)]))
    with pytest.raises(SummaryCompletedError):
        candidate.page(candidate.generation, 0)


def test_host_result_must_match_the_staged_candidate() -> None:
    candidate = _candidate_with([_message(1)])
    candidate.submit_final({"content": "真实总结"})

    assert candidate.verify_host_result({"summary_text": "真实总结"}) == "真实总结"
    for result in (
        {"summary_text": "伪造总结"},
        {"summary_text": "真实总结", "extra": 1},
        {"content": "真实总结"},
        {},
    ):
        with pytest.raises(SummaryResultMismatchError):
            candidate.verify_host_result(cast("dict[str, object]", result))


def test_selection_descriptor_exposes_metadata_and_first_page_cursor() -> None:
    metadata = HistoryFetchMetadata(
        status="partial",
        requested_count=10,
        retrieved_item_count=6,
        missing_count=4,
        completed_batches=1,
        failed_batch=2,
        failure_code="history_api_error",
    )
    candidate = _candidate_with(
        [_message(1)],
        source="messages_by_topic",
        matched_count=3,
        metadata=metadata,
    )

    descriptor = candidate.descriptor()

    assert descriptor["generation"] == candidate.generation
    assert descriptor["source"] == "messages_by_topic"
    assert descriptor["matched_count"] == 3
    assert descriptor["selected_count"] == 1
    assert descriptor["first_page_offset"] == 0
    assert descriptor["history_fetch"]["status"] == "partial"
    assert descriptor["filters"] == {"count": 10}
