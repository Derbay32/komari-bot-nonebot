"""TSK-206 chat/summary prompt and session isolation under concurrency."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from komari_bot.agent import (
    AgentTaskBudget,
    AgentTaskRequest,
    JsonValue,
    TaskKind,
)
from komari_bot.plugins.group_history_summary.dsh_summary_service import (
    DshSummaryService,
)
from komari_bot.plugins.group_history_summary.history_service import HistoryMessage
from komari_bot.plugins.group_history_summary.history_tools import SummaryToolResult
from komari_bot.plugins.group_history_summary.summary_task_data import (
    FrozenSummaryTaskConfig,
    SummaryLimits,
    SummaryTaskBudget,
)
from tests.komari_agent.dsh_test_support import (
    MAIN_HOST_SCRIPT,
    ModelRequest,
    ScriptedResponse,
    TextResponse,
    ToolCallResponse,
    chat_task_data,
    running_dsh,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

pytestmark = pytest.mark.asyncio


class _Provider:
    async def fetch_recent(self, arguments: Mapping[str, object]) -> SummaryToolResult:
        del arguments
        return SummaryToolResult(
            source="recent_group_messages",
            matched_count=1,
            messages=[
                HistoryMessage(
                    user_id="u-1",
                    nickname="阿明",
                    content="并发历史消息",
                    timestamp=1,
                    message_seq=1,
                    message_id="m-1",
                    reply_to_message_id=None,
                )
            ],
            filters={"count": 10},
            history_fetch=None,
        )

    async def fetch_user(self, arguments: Mapping[str, object]) -> SummaryToolResult:
        del arguments
        raise AssertionError

    async def fetch_topic(self, arguments: Mapping[str, object]) -> SummaryToolResult:
        del arguments
        raise AssertionError


def _frozen() -> FrozenSummaryTaskConfig:
    return FrozenSummaryTaskConfig(
        budget=SummaryTaskBudget(rounds=4, per_round=1, total=4),
        limits=SummaryLimits(
            default_count=30,
            min_count=10,
            max_count=200,
            scan_limit=300,
        ),
        fetch_batch_size=50,
        history_min_coverage_ratio=0.8,
        layout_params={"canvas_width": 1365},
        redis_db=0,
        lock_ttl_seconds=300,
    )


def _chat_callbacks() -> tuple[dict[str, Any], dict[str, object]]:
    state: dict[str, object] = {}

    async def admission(_request: Any) -> dict[str, bool]:
        return {"allow": True}

    def missing() -> list[str]:
        values: list[str] = []
        if "content" not in state:
            values.append("final_response")
        if "delta" not in state or "reason" not in state:
            values.append("record_favorability_delta")
        return values

    def complete() -> dict[str, object] | None:
        if "content" in state and "delta" in state and "reason" in state:
            return {
                "content": state["content"],
                "favorability_delta": state["delta"],
                "favorability_reason": state["reason"],
            }
        return None

    async def submit_final(request: Any) -> dict[str, object]:
        content = request.data.get("content")
        if not isinstance(content, str) or not content.strip():
            return {
                "accepted": False,
                "code": "invalid_content",
                "missing": missing(),
                "result": None,
            }
        state["content"] = content.strip()
        return {
            "accepted": True,
            "code": None,
            "missing": missing(),
            "result": complete(),
        }

    async def submit_favor(request: Any) -> dict[str, object]:
        delta = request.data.get("delta")
        reason = request.data.get("reason")
        if not isinstance(delta, int) or isinstance(delta, bool) or not isinstance(reason, str):
            return {
                "accepted": False,
                "code": "invalid_favorability",
                "missing": missing(),
                "result": None,
            }
        state["delta"] = delta
        state["reason"] = reason
        return {
            "accepted": True,
            "code": None,
            "missing": missing(),
            "result": complete(),
        }

    return {
        "admission": admission,
        "chat.submit_final_response": submit_final,
        "chat.submit_favorability": submit_favor,
    }, state


async def test_chat_and_summary_prompts_tools_and_sessions_are_isolated(
    tmp_path: Path,
) -> None:
    counts = {"chat": 0, "summary": 0}

    def responder(request: ModelRequest) -> ScriptedResponse:
        if "Frozen chat role." in request.system_text:
            counts["chat"] += 1
            if counts["chat"] == 1:
                return ToolCallResponse("final_response", {"content": "并发聊天回复"})
            return ToolCallResponse(
                "record_favorability_delta",
                {"delta": 1, "reason": "并发提交"},
            )
        if "Frozen summary role." in request.system_text:
            counts["summary"] += 1
            if counts["summary"] == 1:
                return ToolCallResponse(
                    "fetch_recent_group_messages",
                    {"count": 10},
                )
            return ToolCallResponse("final_summary", {"content": "并发总结"})
        return TextResponse("unexpected role prompt")

    async with running_dsh(
        tmp_path,
        responses=(),
        responder=responder,
        entry_script=MAIN_HOST_SCRIPT,
        prompt="Frozen chat role.",
        summary_prompt="Frozen summary role.",
        context_window=32768,
    ) as (host, server):
        callbacks, state = _chat_callbacks()
        summary_service = DshSummaryService(
            host_getter=lambda: host,
            history_provider_factory=lambda *_args: _Provider(),
            admission_checker=lambda _group_id: True,
        )
        chat_request = AgentTaskRequest(
            task_id="concurrent-chat",
            kind=TaskKind.CHAT,
            task_data=cast("Mapping[str, JsonValue]", chat_task_data("并发问题")),
            budget=AgentTaskBudget(
                max_rounds=3,
                max_tool_calls_per_round=1,
                max_total_tool_calls=3,
            ),
        )
        chat_task = asyncio.create_task(host.execute(chat_request, callbacks))
        summary_task = asyncio.create_task(
            summary_service.generate(
                bot=SimpleNamespace(),
                group_id="123",
                bot_self_id="999",
                user_request="并发总结请求",
                request_trace_id="concurrent-summary",
                config=_frozen(),
            )
        )
        chat_result, summary_result = await asyncio.gather(chat_task, summary_task)

    assert state["content"] == "并发聊天回复"
    assert chat_result.result["content"] == "并发聊天回复"
    assert summary_result.summary_text == "并发总结"
    assert counts == {"chat": 2, "summary": 2}
    assert len(server.requests) == 4

    chat_requests = [r for r in server.requests if "Frozen chat role." in r.system_text]
    summary_requests = [r for r in server.requests if "Frozen summary role." in r.system_text]
    assert len(chat_requests) == 2
    assert len(summary_requests) == 2
    for request in chat_requests:
        assert "Frozen summary role." not in request.system_text
        assert "komari_chat_security_boundary" in request.system_text
    for request in summary_requests:
        assert "Frozen chat role." not in request.system_text
        assert "komari_summary_security_boundary" in request.system_text

    assert set(chat_requests[0].tool_names) == {
        "final_response",
        "record_favorability_delta",
        "recall_memory",
        "search_knowledge",
        "search_web",
        "fetch_page",
    }
    assert set(summary_requests[0].tool_names) == {
        "fetch_recent_group_messages",
        "fetch_messages_by_user",
        "fetch_messages_by_topic",
        "final_summary",
    }

    chat_input = chat_requests[0].user_texts
    summary_input = summary_requests[0].user_texts
    assert len(chat_input) == 1
    assert len(summary_input) == 1
    assert chat_input[0].startswith('<komari_chat_task schema="1">')
    assert summary_input[0].startswith('<komari_summary_task schema="1">')
    assert "并发总结请求" not in chat_input[0]
    assert "并发问题" not in summary_input[0]

    chat_tool_history = "\n".join(
        str(message.get("content"))
        for request in chat_requests[1:]
        for message in request.messages
        if message.get("role") == "tool"
    )
    summary_tool_history = "\n".join(
        str(message.get("content"))
        for request in summary_requests[1:]
        for message in request.messages
        if message.get("role") == "tool"
    )
    assert "final_response" in chat_tool_history or "accepted" in chat_tool_history
    assert "<untrusted_context" in summary_tool_history
    assert "并发历史消息" in summary_tool_history
    assert "并发历史消息" not in chat_tool_history
