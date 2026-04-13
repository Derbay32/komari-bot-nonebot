"""group_history_summary 规划服务测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from komari_bot.plugins.group_history_summary.history_service import HistoryMessage
from komari_bot.plugins.group_history_summary.planner_service import (
    USER_SOURCE,
    build_summary_tools,
    plan_summary_request,
)

if TYPE_CHECKING:
    from nonebot.adapters.onebot.v11 import Bot
from komari_bot.plugins.llm_provider.base_client import (
    LLMCompletionResultSchema,
    LLMToolCallFunctionSchema,
    LLMToolCallSchema,
)


def _build_history_message(
    *,
    user_id: str,
    nickname: str,
    content: str,
    timestamp: int,
    message_seq: int,
) -> HistoryMessage:
    return HistoryMessage(
        user_id=user_id,
        nickname=nickname,
        content=content,
        timestamp=timestamp,
        message_seq=message_seq,
        message_id=str(message_seq),
        reply_to_message_id=None,
    )


@pytest.mark.asyncio
async def test_plan_summary_request_uses_recent_messages_tool(monkeypatch: Any) -> None:
    import komari_bot.plugins.group_history_summary.planner_service as planner_module

    completions = [
        LLMCompletionResultSchema(
            content="",
            tool_calls=[
                LLMToolCallSchema(
                    id="call_1",
                    function=LLMToolCallFunctionSchema(
                        name="fetch_recent_group_messages",
                        arguments='{"count": 50}',
                    ),
                    raw_arguments='{"count": 50}',
                    parsed_arguments={"count": 50},
                )
            ],
            finish_reason="tool_calls",
        ),
        LLMCompletionResultSchema(
            content="规划完成", tool_calls=[], finish_reason="stop"
        ),
    ]

    async def _fake_generate_messages_completion(
        **_kwargs: Any,
    ) -> LLMCompletionResultSchema:
        return completions.pop(0)

    async def _fake_fetch_history_window(**kwargs: Any) -> list[HistoryMessage]:
        assert kwargs["count"] == 50
        return [
            _build_history_message(
                user_id="1001",
                nickname="阿明",
                content="今天先发测试包吧",
                timestamp=1,
                message_seq=1,
            )
        ]

    monkeypatch.setattr(
        planner_module.llm_provider,
        "generate_messages_completion",
        _fake_generate_messages_completion,
    )
    monkeypatch.setattr(
        planner_module, "_fetch_history_window", _fake_fetch_history_window
    )

    result = await plan_summary_request(
        bot=cast("Bot", SimpleNamespace()),
        group_id="123",
        bot_self_id="999",
        user_request="总结最近 50 条",
        planning_model="deepseek-chat",
        planning_max_tokens=800,
        planning_round_limit=3,
        summary_default_count=50,
        min_summary_count=10,
        max_summary_count=200,
        summary_tool_scan_limit=300,
        fetch_batch_size=50,
    )

    assert len(result.messages) == 1
    assert result.tool_result is not None
    assert result.tool_result.filters["count"] == 50


@pytest.mark.asyncio
async def test_plan_summary_request_uses_user_filter_tool(monkeypatch: Any) -> None:
    import komari_bot.plugins.group_history_summary.planner_service as planner_module

    completions = [
        LLMCompletionResultSchema(
            content="",
            tool_calls=[
                LLMToolCallSchema(
                    id="call_1",
                    function=LLMToolCallFunctionSchema(
                        name="fetch_messages_by_user",
                        arguments='{"display_name": "阿明", "count": 20, "scan_limit": 80}',
                    ),
                    raw_arguments='{"display_name": "阿明", "count": 20, "scan_limit": 80}',
                    parsed_arguments={
                        "display_name": "阿明",
                        "count": 20,
                        "scan_limit": 80,
                    },
                )
            ],
            finish_reason="tool_calls",
        ),
        LLMCompletionResultSchema(
            content="规划完成", tool_calls=[], finish_reason="stop"
        ),
    ]

    async def _fake_generate_messages_completion(
        **_kwargs: Any,
    ) -> LLMCompletionResultSchema:
        return completions.pop(0)

    async def _fake_fetch_history_window(**kwargs: Any) -> list[HistoryMessage]:
        assert kwargs["count"] == 80
        return [
            _build_history_message(
                user_id="1001",
                nickname="阿明",
                content="今天先发测试包吧",
                timestamp=1,
                message_seq=1,
            ),
            _build_history_message(
                user_id="1002",
                nickname="小红",
                content="收到",
                timestamp=2,
                message_seq=2,
            ),
        ]

    monkeypatch.setattr(
        planner_module.llm_provider,
        "generate_messages_completion",
        _fake_generate_messages_completion,
    )
    monkeypatch.setattr(
        planner_module, "_fetch_history_window", _fake_fetch_history_window
    )

    result = await plan_summary_request(
        bot=cast("Bot", SimpleNamespace()),
        group_id="123",
        bot_self_id="999",
        user_request="总结一下阿明最近说了什么",
        planning_model="deepseek-chat",
        planning_max_tokens=800,
        planning_round_limit=3,
        summary_default_count=50,
        min_summary_count=10,
        max_summary_count=200,
        summary_tool_scan_limit=300,
        fetch_batch_size=50,
    )

    assert result.tool_result is not None
    assert result.tool_result.source == USER_SOURCE
    assert len(result.messages) == 1
    assert result.messages[0].nickname == "阿明"


@pytest.mark.asyncio
async def test_plan_summary_request_falls_back_after_round_limit(
    monkeypatch: Any,
) -> None:
    import komari_bot.plugins.group_history_summary.planner_service as planner_module

    completion = LLMCompletionResultSchema(
        content="",
        tool_calls=[
            LLMToolCallSchema(
                id="call_1",
                function=LLMToolCallFunctionSchema(
                    name="fetch_recent_group_messages",
                    arguments='{"count": 20}',
                ),
                raw_arguments='{"count": 20}',
                parsed_arguments={"count": 20},
            )
        ],
        finish_reason="tool_calls",
    )

    async def _fake_generate_messages_completion(
        **_kwargs: Any,
    ) -> LLMCompletionResultSchema:
        return completion

    async def _fake_fetch_history_window(**kwargs: Any) -> list[HistoryMessage]:
        return [
            _build_history_message(
                user_id="1001",
                nickname="阿明",
                content=f"消息{kwargs['count']}",
                timestamp=1,
                message_seq=1,
            )
        ]

    monkeypatch.setattr(
        planner_module.llm_provider,
        "generate_messages_completion",
        _fake_generate_messages_completion,
    )
    monkeypatch.setattr(
        planner_module, "_fetch_history_window", _fake_fetch_history_window
    )

    result = await plan_summary_request(
        bot=cast("Bot", SimpleNamespace()),
        group_id="123",
        bot_self_id="999",
        user_request="总结最近消息",
        planning_model="deepseek-chat",
        planning_max_tokens=800,
        planning_round_limit=1,
        summary_default_count=30,
        min_summary_count=10,
        max_summary_count=200,
        summary_tool_scan_limit=300,
        fetch_batch_size=50,
    )

    assert result.tool_result is not None
    assert result.rounds_used == 1
    assert "上限" in result.planner_note


def test_build_summary_tools_contains_expected_functions() -> None:
    tool_names = [tool["function"]["name"] for tool in build_summary_tools()]
    assert tool_names == [
        "fetch_recent_group_messages",
        "fetch_messages_by_user",
        "fetch_messages_by_topic",
    ]
