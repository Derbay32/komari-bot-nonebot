"""群总结历史工具业务规则（无模型/提示词依赖）。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from komari_bot.plugins.group_history_summary.history_service import (
    HistoryFetchMetadata,
    HistoryFetchResult,
    HistoryIncompleteError,
    HistoryMessage,
)
from komari_bot.plugins.group_history_summary.history_tools import (
    RECENT_SOURCE,
    TOPIC_SOURCE,
    USER_SOURCE,
    execute_recent_tool,
    execute_topic_tool,
    execute_user_tool,
    fetch_history_window,
)

BOT_ID = "999"


def _message(
    seq: int,
    *,
    user_id: str = "1001",
    nickname: str = "阿明",
    content: str = "消息",
    reply_to: str | None = None,
) -> HistoryMessage:
    return HistoryMessage(
        user_id=user_id,
        nickname=nickname,
        content=content,
        timestamp=seq,
        message_seq=seq,
        message_id=f"m{seq}",
        reply_to_message_id=reply_to,
    )


def _fetch_result(
    messages: list[HistoryMessage],
    *,
    requested_count: int | None = None,
    metadata: HistoryFetchMetadata | None = None,
) -> HistoryFetchResult:
    return HistoryFetchResult(
        messages=messages,
        metadata=metadata
        or HistoryFetchMetadata(
            status="complete",
            requested_count=requested_count or len(messages),
            retrieved_item_count=len(messages),
            missing_count=0,
            completed_batches=1,
        ),
    )


def _patch_fetch(
    monkeypatch: pytest.MonkeyPatch,
    result: HistoryFetchResult,
    calls: list[dict[str, Any]] | None = None,
) -> None:
    harness = calls if calls is not None else []

    async def _fake_fetch_window(**kwargs: Any) -> HistoryFetchResult:
        harness.append(dict(kwargs))
        return result

    monkeypatch.setattr(
        "komari_bot.plugins.group_history_summary.history_tools.fetch_history_window",
        _fake_fetch_window,
    )


@pytest.mark.asyncio
async def test_recent_tool_clamps_count_and_filters_commands_and_bot_replies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = _message(1, content="总结一下50条")
    bot_reply = _message(2, user_id=BOT_ID, content="好的", reply_to=command.message_id)
    user_message = _message(3, content="普通消息")
    calls: list[dict[str, Any]] = []
    _patch_fetch(
        monkeypatch,
        _fetch_result([command, bot_reply, user_message]),
        calls,
    )

    result = await execute_recent_tool(
        bot=cast("Any", SimpleNamespace()),
        group_id="123",
        bot_self_id=BOT_ID,
        batch_size=50,
        min_summary_count=10,
        max_summary_count=200,
        history_min_coverage_ratio=0.8,
        arguments={"count": 2, "include_bot_replies": False},
    )

    assert calls[0]["count"] == 10
    assert result.source == RECENT_SOURCE
    assert [message.content for message in result.messages] == ["普通消息"]
    assert result.matched_count == 1
    assert result.filters == {"count": 10, "include_bot_replies": False}


@pytest.mark.asyncio
async def test_recent_tool_include_bot_replies_keeps_non_command_bot_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = _message(1, content="总结一下50条")
    bot_reply = _message(2, user_id=BOT_ID, content="好的", reply_to=command.message_id)
    plain_bot = _message(3, user_id=BOT_ID, content="机器人闲聊")
    user_message = _message(4, content="普通消息")
    _patch_fetch(monkeypatch, _fetch_result([command, bot_reply, plain_bot, user_message]))

    result = await execute_recent_tool(
        bot=cast("Any", SimpleNamespace()),
        group_id="123",
        bot_self_id=BOT_ID,
        batch_size=50,
        min_summary_count=10,
        max_summary_count=200,
        history_min_coverage_ratio=0.8,
        arguments={"count": 10, "include_bot_replies": True},
    )

    assert [message.content for message in result.messages] == ["好的", "机器人闲聊", "普通消息"]


@pytest.mark.asyncio
async def test_user_tool_matches_user_id_and_display_name_with_scan_clamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = [
        _message(1, user_id="1001", nickname="阿明"),
        _message(2, user_id="1002", nickname="小红"),
        _message(3, user_id="1003", nickname="阿明同学"),
    ]
    calls: list[dict[str, Any]] = []
    _patch_fetch(monkeypatch, _fetch_result(messages), calls)

    by_name = await execute_user_tool(
        bot=cast("Any", SimpleNamespace()),
        group_id="123",
        bot_self_id=BOT_ID,
        batch_size=50,
        min_summary_count=10,
        max_summary_count=200,
        summary_tool_scan_limit=300,
        history_min_coverage_ratio=0.8,
        arguments={"count": 20, "display_name": "阿明", "scan_limit": 5},
    )
    by_id = await execute_user_tool(
        bot=cast("Any", SimpleNamespace()),
        group_id="123",
        bot_self_id=BOT_ID,
        batch_size=50,
        min_summary_count=10,
        max_summary_count=200,
        summary_tool_scan_limit=300,
        history_min_coverage_ratio=0.8,
        arguments={"count": 20, "user_id": "1002"},
    )

    assert calls[0]["count"] == 20
    assert by_name.source == USER_SOURCE
    assert [message.user_id for message in by_name.messages] == ["1001", "1003"]
    assert by_name.filters["scan_limit"] == 20
    assert [message.user_id for message in by_id.messages] == ["1002"]

    long_name = "x" * 40
    normalized = await execute_user_tool(
        bot=cast("Any", SimpleNamespace()),
        group_id="123",
        bot_self_id=BOT_ID,
        batch_size=50,
        min_summary_count=10,
        max_summary_count=200,
        summary_tool_scan_limit=300,
        history_min_coverage_ratio=0.8,
        arguments={"count": 20, "display_name": long_name},
    )
    assert normalized.filters["display_name"] == "x" * 32


@pytest.mark.asyncio
async def test_topic_tool_deduplicates_caps_and_normalizes_keywords(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = [
        _message(1, content="alpha 讨论"),
        _message(2, content="BETA 讨论"),
        _message(3, content="其他"),
    ]
    calls: list[dict[str, Any]] = []
    _patch_fetch(monkeypatch, _fetch_result(messages), calls)

    keywords = ["alpha", "alpha", "beta", "gamma", "delta", "epsilon", "zeta"]
    result = await execute_topic_tool(
        bot=cast("Any", SimpleNamespace()),
        group_id="123",
        bot_self_id=BOT_ID,
        batch_size=50,
        min_summary_count=10,
        max_summary_count=200,
        summary_tool_scan_limit=300,
        history_min_coverage_ratio=0.8,
        arguments={"count": 20, "keywords": keywords, "scan_limit": 300},
    )

    assert calls[0]["count"] == 300
    assert result.source == TOPIC_SOURCE
    assert result.filters["keywords"] == ["alpha", "beta", "gamma", "delta", "epsilon"]
    assert [message.message_seq for message in result.messages] == [1, 2]

    long_keyword = "y" * 40
    long_result = await execute_topic_tool(
        bot=cast("Any", SimpleNamespace()),
        group_id="123",
        bot_self_id=BOT_ID,
        batch_size=50,
        min_summary_count=10,
        max_summary_count=200,
        summary_tool_scan_limit=300,
        history_min_coverage_ratio=0.8,
        arguments={"count": 20, "keywords": [long_keyword]},
    )
    assert long_result.filters["keywords"] == ["y" * 32]


@pytest.mark.asyncio
async def test_partial_history_below_threshold_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    partial = _fetch_result(
        [_message(1)],
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
    _patch_fetch(monkeypatch, partial)

    with pytest.raises(HistoryIncompleteError):
        await execute_recent_tool(
            bot=cast("Any", SimpleNamespace()),
            group_id="123",
            bot_self_id=BOT_ID,
            batch_size=50,
            min_summary_count=10,
            max_summary_count=200,
            history_min_coverage_ratio=0.8,
            arguments={"count": 10},
        )


@pytest.mark.asyncio
async def test_allowed_partial_history_keeps_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    partial = _fetch_result(
        [_message(1)],
        metadata=HistoryFetchMetadata(
            status="partial",
            requested_count=10,
            retrieved_item_count=6,
            missing_count=4,
            completed_batches=1,
            failed_batch=2,
            failure_code="history_api_error",
        ),
    )
    _patch_fetch(monkeypatch, partial)

    result = await execute_recent_tool(
        bot=cast("Any", SimpleNamespace()),
        group_id="123",
        bot_self_id=BOT_ID,
        batch_size=50,
        min_summary_count=10,
        max_summary_count=200,
        history_min_coverage_ratio=0.5,
        arguments={"count": 10},
    )

    assert result.history_fetch is not None
    assert result.history_fetch.status == "partial"
    assert result.history_fetch.failed_batch == 2


@pytest.mark.asyncio
async def test_fetch_history_window_uses_group_scoped_names_and_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission_calls: list[str] = []
    resolved: list[tuple[str, str]] = []

    def _admit(**kwargs: Any) -> None:
        admission_calls.append(str(kwargs["group_id"]))

    def _resolve(
        *,
        group_id: str,
        user_id: str,
        fallback_nickname: str | None,
    ) -> str:
        resolved.append((group_id, user_id))
        return f"角色-{fallback_nickname}"

    async def _fetch(**kwargs: Any) -> HistoryFetchResult:
        assert kwargs["name_resolver"] is _resolve
        return _fetch_result([_message(1, nickname="角色-平台名")])

    monkeypatch.setattr(
        "komari_bot.plugins.group_history_summary.history_tools.ensure_effect_admitted",
        _admit,
    )
    monkeypatch.setattr(
        "komari_bot.plugins.group_history_summary.history_tools.character_binding.get_character_name",
        _resolve,
    )
    monkeypatch.setattr(
        "komari_bot.plugins.group_history_summary.history_tools.fetch_group_history_messages",
        _fetch,
    )

    result = await fetch_history_window(
        bot=cast("Any", SimpleNamespace()),
        group_id="123",
        count=10,
        batch_size=50,
    )

    assert admission_calls == ["123"]
    assert resolved == []
    assert result.messages[0].nickname == "角色-平台名"
