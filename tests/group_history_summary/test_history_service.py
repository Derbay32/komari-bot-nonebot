"""群总结历史分页完整度测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from komari_bot.plugins.group_history_summary.history_service import (
    HistoryFetchMetadata,
    HistoryFetchResult,
    HistoryIncompleteError,
    HistoryMessage,
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
        name_resolver=lambda _user_id, nickname: nickname,
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
        name_resolver=lambda _user_id, nickname: nickname,
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
        name_resolver=lambda _user_id, nickname: nickname,
    )

    assert result.metadata.status == "partial"
    assert result.metadata.failure_code == "history_pagination_stalled"
    assert result.metadata.failed_batch == 2
    assert result.metadata.retrieved_item_count == 2


def _partial_fetch_result() -> HistoryFetchResult:
    return HistoryFetchResult(
        messages=[
            HistoryMessage(
                user_id="1001",
                nickname="用户1",
                content="消息1",
                timestamp=1,
                message_seq=1,
                message_id="1",
                reply_to_message_id=None,
            )
        ],
        metadata=HistoryFetchMetadata(
            status="partial",
            requested_count=10,
            retrieved_item_count=5,
            missing_count=5,
            completed_batches=1,
            failed_batch=2,
            failure_code="history_api_error",
        ),
    )


@pytest.mark.asyncio
async def test_planner_rejects_partial_history_below_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import komari_bot.plugins.group_history_summary.planner_service as planner_module

    async def _partial_fetch(**_kwargs: object) -> HistoryFetchResult:
        return _partial_fetch_result()

    monkeypatch.setattr(planner_module, "_fetch_history_window", _partial_fetch)

    with pytest.raises(HistoryIncompleteError):
        await planner_module._execute_recent_tool(
            bot=cast("Any", SimpleNamespace()),
            group_id="10000",
            bot_self_id="999",
            batch_size=10,
            min_summary_count=1,
            max_summary_count=20,
            history_min_coverage_ratio=0.8,
            arguments={"count": 10},
        )


@pytest.mark.asyncio
async def test_planner_preserves_allowed_partial_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import komari_bot.plugins.group_history_summary.planner_service as planner_module

    async def _partial_fetch(**_kwargs: object) -> HistoryFetchResult:
        return _partial_fetch_result()

    monkeypatch.setattr(planner_module, "_fetch_history_window", _partial_fetch)

    result = await planner_module._execute_recent_tool(
        bot=cast("Any", SimpleNamespace()),
        group_id="10000",
        bot_self_id="999",
        batch_size=10,
        min_summary_count=1,
        max_summary_count=20,
        history_min_coverage_ratio=0.5,
        arguments={"count": 10},
    )

    assert result.history_fetch is not None
    assert result.history_fetch.status == "partial"
    assert result.history_fetch.failed_batch == 2
