"""群总结历史分页完整度测试。"""

from __future__ import annotations

from typing import Any, cast

import pytest

from komari_bot.plugins.group_history_summary.history_service import (
    fetch_group_history_messages,
)


def _raw_message(seq: int) -> dict[str, object]:
    return {
        "user_id": str(1000 + seq),
        "time": seq,
        "message_seq": seq,
        "message_id": seq,
        "sender": {"nickname": f"用户{seq}"},
        "message": [{"type": "text", "data": {"text": f"消息{seq}"}}],
    }


class _HistoryBot:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)

    async def call_api(self, _name: str, **_kwargs: object) -> object:
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _identity_name(
    *,
    group_id: str,
    user_id: str,
    fallback_nickname: str,
) -> str:
    del group_id, user_id
    return fallback_nickname


@pytest.mark.asyncio
async def test_fetch_history_marks_partial_and_failed_batch() -> None:
    bot = _HistoryBot(
        [
            {"messages": [_raw_message(4), _raw_message(3)]},
            RuntimeError("平台暂时不可用"),
        ]
    )

    result = await fetch_group_history_messages(
        bot=cast("Any", bot),
        group_id="10000",
        count=4,
        batch_size=2,
        name_resolver=_identity_name,
    )

    assert [message.message_seq for message in result.messages] == [3, 4]
    assert result.metadata.status == "partial"
    assert result.metadata.retrieved_item_count == 2
    assert result.metadata.missing_count == 2
    assert result.metadata.completed_batches == 1
    assert result.metadata.failed_batch == 2
    assert result.metadata.failure_code == "history_api_error"
    assert result.metadata.coverage_ratio == 0.5


@pytest.mark.asyncio
async def test_fetch_history_natural_end_is_complete() -> None:
    bot = _HistoryBot(
        [
            {"messages": [_raw_message(2), _raw_message(1)]},
            {"messages": []},
        ]
    )

    result = await fetch_group_history_messages(
        bot=cast("Any", bot),
        group_id="10000",
        count=4,
        batch_size=2,
        name_resolver=_identity_name,
    )

    assert result.metadata.status == "complete"
    assert result.metadata.coverage_ratio == 1.0
    assert result.metadata.missing_count == 0
    assert result.metadata.completed_batches == 1


@pytest.mark.asyncio
async def test_fetch_history_marks_repeated_page_as_partial() -> None:
    repeated_page = {"messages": [_raw_message(4), _raw_message(3)]}
    bot = _HistoryBot([repeated_page, repeated_page])

    result = await fetch_group_history_messages(
        bot=cast("Any", bot),
        group_id="10000",
        count=4,
        batch_size=2,
        name_resolver=_identity_name,
    )

    assert result.metadata.status == "partial"
    assert result.metadata.failure_code == "history_pagination_stalled"
    assert result.metadata.failed_batch == 2
    assert result.metadata.retrieved_item_count == 2
