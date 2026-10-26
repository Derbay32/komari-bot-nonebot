"""TSK-203 Python service over the real installed DSH chat preset."""

from __future__ import annotations

from importlib import import_module
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from komari_bot.agent import AgentTaskError
from komari_bot.context.redis_store import MessageSchema
from komari_bot.plugins.komari_chat.services.reply_context import ReplyContext
from tests.komari_agent.dsh_test_support import (
    MAIN_HOST_SCRIPT,
    ToolCallResponse,
    running_dsh,
)
from tests.komari_chat.hindsight_recall_support import (
    FakeHindsightMemory,
    fake_recall_session,
    recall_response,
)

service_module = import_module(
    "komari_bot.plugins.komari_chat.services.dsh_chat_service"
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.asyncio


def _message(index: int, *, content: str | None = None) -> MessageSchema:
    return MessageSchema(
        user_id=f"u-{index}",
        user_nickname=f"用户{index}",
        group_id="g-1",
        content=content if content is not None else f"消息{index}",
        timestamp=float(index),
        message_id=f"m-{index}",
    )


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        bot_nickname="小鞠",
        knowledge_enabled=False,
        knowledge_limit=3,
        agent_max_rounds=4,
        agent_max_tool_calls_per_round=2,
        agent_max_total_tool_calls=4,
        vision_image_download_max_count=8,
        vision_image_download_max_bytes=16 * 1024 * 1024,
        vision_image_download_total_max_bytes=32 * 1024 * 1024,
        vision_image_download_max_pixels=100_000_000,
        vision_image_download_concurrency=2,
        vision_image_download_connect_timeout_seconds=5.0,
        vision_image_download_read_timeout_seconds=30.0,
        vision_image_download_total_timeout_seconds=45.0,
    )


async def _favorability(_uid: str) -> object:
    return SimpleNamespace(
        user_id="u-0",
        favorability=7,
        stage_index=1,
        stage_name="熟悉",
        stage_prompt="关系逐渐熟悉。",
    )


async def _empty_search(**_kwargs: object) -> list[object]:
    return []


async def _empty_uid(_uid: str) -> list[object]:
    return []


def _group_bindings(_group_id: str) -> dict[str, str]:
    return {"u-0": "管理员"}


async def test_real_dsh_chat_preset_returns_validated_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production main entry runs the real chat preset and tool loop."""
    responses = (
        ToolCallResponse("final_response", {"content": "真实 DSH 回复"}),
        ToolCallResponse(
            "record_favorability_delta",
            {"delta": 1, "reason": "真实工具提交"},
        ),
    )
    memory = FakeHindsightMemory(response=recall_response())
    monkeypatch.setattr(
        service_module,
        "_user_data_config",
        lambda: SimpleNamespace(max_favorability_delta_per_reply=3),
    )

    async with running_dsh(
        tmp_path,
        responses=responses,
        entry_script=MAIN_HOST_SCRIPT,
        prompt="冻结的聊天角色提示词。",
    ) as (host, server):
        service = service_module.DshChatService(
            host_getter=lambda: host,
            knowledge_search=_empty_search,
            uid_search=_empty_uid,
            favorability_getter=_favorability,
            group_bindings_getter=_group_bindings,
        )
        quote = ReplyContext(
            source_side="user",
            message_id="quoted-1",
            user_id="u-2",
            user_nickname="被引用用户",
            text="引用正文",
            image_sources=(),
            image_count=0,
            has_visible_image=False,
        )

        result = await service.generate(
            message=_message(0, content="当前真实消息"),
            recent_messages=[_message(1), _message(2)],
            reply_context=quote,
            recall_session=fake_recall_session(memory),
            request_trace_id="chat-real-1",
            config=_config(),
            caller_is_superuser=False,
            image_urls=None,
        )

    assert result.content == "真实 DSH 回复"
    assert result.favorability_delta == 1
    assert result.favorability_reason == "真实工具提交"
    assert len(memory.recall_calls) == 1
    assert "当前真实消息" in memory.recall_calls[0]
    assert len(server.requests) == 2

    first = server.requests[0]
    assert set(first.tool_names) == {"final_response", "record_favorability_delta"}
    assert "冻结的聊天角色提示词。" in first.system_text
    assert "komari_chat_security_boundary" in first.system_text
    assert len(first.user_texts) == 1
    task_input = first.user_texts[0]
    assert task_input.startswith('<komari_chat_task schema="1">')
    assert "<current_message>" in task_input
    assert "<recent_messages>" in task_input
    assert "<quote>" in task_input
    assert '"content":"当前真实消息"' in task_input
    assert '"content":"引用正文"' in task_input
    # The quote relationship is data, never a reconstructed assistant turn.
    assert not any(message.get("role") == "assistant" for message in first.messages)
    assert "引用正文" not in first.system_text

    second = server.requests[1]
    # The second step stays inside the same DSH session/task; the first tool
    # result and the original partition remain in the request history.
    assert any(message.get("role") == "tool" for message in second.messages)


class _RedisBuffer:
    def __init__(self, messages: list[MessageSchema]) -> None:
        self.messages = messages
        self.pushed: list[MessageSchema] = []

    async def get_buffer(self, _group_id: str, limit: int = 100) -> list[MessageSchema]:
        del limit
        return list(self.messages)

    async def push_message(self, _group_id: str, message: MessageSchema) -> None:
        self.pushed.append(message)


class _UnusedFulfillment:
    pass


class _UnusedReservation:
    pass


class _UnusedDecision:
    pass


async def test_real_dsh_result_enters_existing_pending_fulfillment_seam(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real DSH result reaches the existing PendingReply fulfillment object."""
    responses = (
        ToolCallResponse("final_response", {"content": "进入履约的回复"}),
        ToolCallResponse(
            "record_favorability_delta",
            {"delta": -1, "reason": "真实的履约前提交"},
        ),
    )
    memory = FakeHindsightMemory(response=recall_response())
    session = fake_recall_session(memory)
    handler_module = import_module(
        "komari_bot.plugins.komari_chat.handlers.message_handler"
    )
    monkeypatch.setattr(
        service_module,
        "_user_data_config",
        lambda: SimpleNamespace(max_favorability_delta_per_reply=3),
    )
    monkeypatch.setattr(handler_module, "get_config", _config)
    monkeypatch.setattr(handler_module, "get_context_config", _config)

    async with running_dsh(
        tmp_path,
        responses=responses,
        entry_script=MAIN_HOST_SCRIPT,
        prompt="冻结的履约角色提示词。",
    ) as (host, _server):
        service = service_module.DshChatService(
            host_getter=lambda: host,
            knowledge_search=_empty_search,
            uid_search=_empty_uid,
            favorability_getter=_favorability,
            group_bindings_getter=_group_bindings,
        )
        handler = handler_module.MessageHandler(
            redis=_RedisBuffer([_message(1)]),
            hindsight_memory=memory,
            reply_fulfillment=_UnusedFulfillment(),
            proactive_reservation=_UnusedReservation(),
            decision_engine=_UnusedDecision(),
            dsh_chat=service,
        )

        pending, stored, failure = await handler._attempt_reply(
            bot_self_id="bot-1",
            adapter_name="OneBot V11",
            message=_message(0, content="履约前的真实消息"),
            reply_to_message_id="m-0",
            image_urls=None,
            reply_context=None,
            reply_context_requested=False,
            reply_context_refetched=False,
            force_reply=True,
            reason="platform_summon",
            recall_session=session,
            store_current=False,
        )

    assert failure is None
    assert stored is False
    assert pending is not None
    assert pending.reply == "进入履约的回复"
    assert pending.reply_result.content == "进入履约的回复"
    assert pending.reply_result.favorability_delta == -1
    assert pending.reply_result.favorability_reason == "真实的履约前提交"
    assert pending.hindsight_target_fingerprint == session.target_fingerprint
    assert pending.reply_result.hindsight_target_fingerprint == session.target_fingerprint
    assert pending.fulfillment_id


class _DebugReservedError(AssertionError):
    """Debug reservation is a forbidden side effect."""


class _ForbiddenReservation:
    async def reserve(self, *_args: object, **_kwargs: object) -> object:
        raise _DebugReservedError


async def test_real_dsh_debug_uses_same_preset_without_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Debug uses the same chat preset and performs no Redis/send/reserve effect."""
    responses = (
        ToolCallResponse("final_response", {"content": "真实 debug 回复"}),
        ToolCallResponse(
            "record_favorability_delta",
            {"delta": 0, "reason": "debug 提交"},
        ),
    )
    memory = FakeHindsightMemory(response=recall_response())
    session = fake_recall_session(memory)
    handler_module = import_module(
        "komari_bot.plugins.komari_chat.handlers.message_handler"
    )
    monkeypatch.setattr(
        service_module,
        "_user_data_config",
        lambda: SimpleNamespace(max_favorability_delta_per_reply=3),
    )
    monkeypatch.setattr(handler_module, "get_config", _config)
    monkeypatch.setattr(handler_module, "get_context_config", _config)

    async with running_dsh(
        tmp_path,
        responses=responses,
        entry_script=MAIN_HOST_SCRIPT,
        prompt="冻结的 debug 角色提示词。",
    ) as (host, _server):
        service = service_module.DshChatService(
            host_getter=lambda: host,
            knowledge_search=_empty_search,
            uid_search=_empty_uid,
            favorability_getter=_favorability,
            group_bindings_getter=_group_bindings,
        )
        redis = _RedisBuffer([_message(1)])
        handler = handler_module.MessageHandler(
            redis=redis,
            hindsight_memory=memory,
            reply_fulfillment=_UnusedFulfillment(),
            proactive_reservation=_ForbiddenReservation(),
            decision_engine=_UnusedDecision(),
            dsh_chat=service,
        )

        result = await handler.generate_debug_reply(
            group_id="g-1",
            user_id="u-debug",
            user_nickname="调试用户",
            content="真实 debug 输入",
            request_trace_id="debug-real-1",
        )

    assert result.reply == "真实 debug 回复"
    assert result.favorability_delta == 0
    assert result.favorability_reason == "debug 提交"
    assert result.request_id == "debug-real-1"
    assert redis.pushed == []
    assert memory.retain_calls == []
    assert session.target_fingerprint


async def _run_service_until_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    responses: tuple[ToolCallResponse, ...],
    allow_admission_checks: int,
) -> tuple[AgentTaskError, FakeHindsightMemory, int]:
    decisions = {"calls": 0}

    def _admission(**_kwargs: object) -> bool:
        decisions["calls"] += 1
        return decisions["calls"] <= allow_admission_checks

    monkeypatch.setattr(service_module, "effect_business_admitted", _admission)
    monkeypatch.setattr(
        service_module,
        "_user_data_config",
        lambda: SimpleNamespace(max_favorability_delta_per_reply=3),
    )
    memory = FakeHindsightMemory(response=recall_response())
    session = fake_recall_session(memory)
    async with running_dsh(
        tmp_path,
        responses=responses,
        entry_script=MAIN_HOST_SCRIPT,
        prompt="冻结的准入角色提示词。",
    ) as (host, server):
        service = service_module.DshChatService(
            host_getter=lambda: host,
            knowledge_search=_empty_search,
            uid_search=_empty_uid,
            favorability_getter=_favorability,
            group_bindings_getter=_group_bindings,
        )
        with pytest.raises(AgentTaskError) as exc_info:
            await service.generate(
                message=_message(0, content="准入提交测试"),
                recent_messages=[],
                reply_context=None,
                recall_session=session,
                request_trace_id="chat-admission-submit",
                config=_config(),
            )
        request_count = len(server.requests)
    assert memory.retain_calls == []
    return exc_info.value, memory, request_count


async def test_real_submission_admission_revoked_before_first_tool_is_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error, memory, request_count = await _run_service_until_terminal(
        tmp_path,
        monkeypatch,
        responses=(
            ToolCallResponse("final_response", {"content": "不应完成"}),
        ),
        allow_admission_checks=3,
    )

    assert error.code == "callback_failed"
    assert len(memory.recall_calls) == 1
    assert request_count == 1


async def test_real_submission_admission_revoked_between_tools_is_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error, memory, request_count = await _run_service_until_terminal(
        tmp_path,
        monkeypatch,
        responses=(
            ToolCallResponse("final_response", {"content": "先提交"}),
            ToolCallResponse(
                "record_favorability_delta",
                {"delta": 1, "reason": "后提交"},
            ),
        ),
        allow_admission_checks=5,
    )

    assert error.code == "callback_failed"
    assert len(memory.recall_calls) == 1
    assert request_count == 2
