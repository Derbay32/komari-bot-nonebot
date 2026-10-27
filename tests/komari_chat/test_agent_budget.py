"""回复 Agent 预算与普通/debug 入口的 DSH 交接契约。"""

from __future__ import annotations

import asyncio
from importlib import import_module
from types import SimpleNamespace
from typing import Any

import pytest

from komari_bot.context.redis_store import MessageSchema
from komari_bot.plugins.komari_chat.config_schema import KomariChatConfigSchema
from tests.komari_chat.hindsight_recall_support import (
    FakeHindsightMemory,
    fake_recall_session,
)

message_handler_module = import_module("komari_bot.plugins.komari_chat.handlers.message_handler")


def _build_chat_config_stub(**overrides: Any) -> SimpleNamespace:
    values = KomariChatConfigSchema(**overrides).model_dump()
    values.update(bot_nickname="小鞠", error_notify_enabled=False)
    return SimpleNamespace(**values)

class _FakeDshChat:
    """Trusted generation boundary double for budget hand-off assertions."""

    def __init__(self, content: str = "预算测试回复") -> None:
        self.content = content
        self.calls: list[dict[str, object]] = []
        self.error: BaseException | None = None

    async def generate(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return import_module(
            "komari_bot.plugins.komari_chat.services.chat_result"
        ).ReplyResult(
            content=self.content,
            favorability_delta=0,
            favorability_reason="预算测试",
        )


def _wire_dsh_handler(
    monkeypatch: pytest.MonkeyPatch,
    config: SimpleNamespace,
) -> tuple[Any, _FakeDshChat]:
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = _FakeRedis()
    handler.hindsight_memory = FakeHindsightMemory()
    handler._reaction_tasks = set()
    service = _FakeDshChat()
    handler.dsh_chat = service
    monkeypatch.setattr(message_handler_module, "get_config", lambda: config)
    monkeypatch.setattr(message_handler_module, "get_context_config", lambda: config)
    return handler, service


class _FakeRedis:
    async def get_buffer(self, group_id: str, limit: int = 100) -> list[MessageSchema]:
        del group_id, limit
        return []

    async def push_message(self, group_id: str, message: MessageSchema) -> None:
        del group_id, message

    async def get_global_interaction_buffer(
        self,
        user_id: str,
        limit: int = 10,
    ) -> list[dict[str, object]]:
        del user_id, limit
        return []


def _make_message(message_id: str = "msg-1") -> MessageSchema:
    return MessageSchema(
        user_id="user-1",
        user_nickname="测试用户",
        group_id="group-1",
        content="测试预算",
        timestamp=1.0,
        message_id=message_id,
    )


def test_normal_entry_passes_frozen_config_to_shared_dsh_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _build_chat_config_stub(agent_max_rounds=2, agent_max_total_tool_calls=8)
    handler, service = _wire_dsh_handler(monkeypatch, config)

    pending, _stored, failure = asyncio.run(
        handler._attempt_reply(
            bot_self_id="bot-1",
            adapter_name="OneBot V11",
            message=_make_message(),
            reply_to_message_id="msg-1",
            image_urls=None,
            reply_context=None,
            reply_context_requested=False,
            reply_context_refetched=False,
            force_reply=True,
            reason="platform_summon",
            recall_session=fake_recall_session(handler.hindsight_memory),
            store_current=True,
        )
    )

    assert failure is None
    assert pending is not None
    assert pending.reply == "预算测试回复"
    assert len(service.calls) == 1
    assert service.calls[0]["config"] is config
    assert service.calls[0]["request_trace_id"] == "chat-msg-1"


def test_debug_entry_passes_frozen_config_to_shared_dsh_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _build_chat_config_stub(agent_max_rounds=2, agent_max_total_tool_calls=8)
    handler, service = _wire_dsh_handler(monkeypatch, config)

    result = asyncio.run(
        handler.generate_debug_reply(
            group_id="group-1",
            user_id="user-1",
            user_nickname="测试用户",
            content="测试预算",
            request_trace_id="debug-budget-1",
        )
    )

    assert result.reply == "预算测试回复"
    assert len(service.calls) == 1
    assert service.calls[0]["config"] is config
    assert service.calls[0]["request_trace_id"] == "debug-budget-1"


def test_chat_task_budget_freezes_values_before_rounds() -> None:
    service_module = import_module(
        "komari_bot.plugins.komari_chat.services.dsh_chat_service"
    )
    config = _build_chat_config_stub(
        agent_max_rounds=8,
        agent_max_tool_calls_per_round=6,
        agent_max_total_tool_calls=30,
    )
    frozen = service_module.ChatTaskBudget.from_config(config)
    config.agent_max_rounds = 2
    config.agent_max_tool_calls_per_round = 1
    config.agent_max_total_tool_calls = 2

    assert frozen.to_agent_budget().to_protocol() == {
        "max_rounds": 8,
        "max_tool_calls_per_round": 6,
        "max_total_tool_calls": 30,
    }


def test_chat_task_budget_rejects_non_integer_or_nonpositive_values() -> None:
    service_module = import_module(
        "komari_bot.plugins.komari_chat.services.dsh_chat_service"
    )
    for values in (
        (0, 1, 2),
        (True, 1, 2),
        (2, 1.5, 2),
        (2, 1, 1_001),
    ):
        config = SimpleNamespace(
            agent_max_rounds=values[0],
            agent_max_tool_calls_per_round=values[1],
            agent_max_total_tool_calls=values[2],
        )
        with pytest.raises(service_module.ChatTaskBudgetError):
            service_module.ChatTaskBudget.from_config(config)


@pytest.mark.parametrize("entry", ["normal", "debug"])
def test_normal_and_debug_entries_share_one_chat_generation_service(
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
) -> None:
    config = _build_chat_config_stub()
    handler, service = _wire_dsh_handler(monkeypatch, config)
    if entry == "debug":
        result = asyncio.run(
            handler.generate_debug_reply(
                group_id="group-1",
                user_id="user-1",
                user_nickname="测试用户",
                content="测试共享入口",
            )
        )
        assert result.reply == "预算测试回复"
    else:
        pending, _stored, failure = asyncio.run(
            handler._attempt_reply(
                bot_self_id="bot-1",
                adapter_name="OneBot V11",
                message=_make_message(),
                reply_to_message_id="msg-1",
                image_urls=None,
                reply_context=None,
                reply_context_requested=False,
                reply_context_refetched=False,
                force_reply=True,
                reason="platform_summon",
                recall_session=fake_recall_session(handler.hindsight_memory),
                store_current=True,
            )
        )
        assert failure is None
        assert pending is not None
    assert len(service.calls) == 1


def test_handler_reports_dsh_task_failure_as_reply_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _build_chat_config_stub()
    handler, service = _wire_dsh_handler(monkeypatch, config)
    service.error = RuntimeError("callback_failed")

    pending, _stored, failure = asyncio.run(
        handler._attempt_reply(
            bot_self_id="bot-1",
            adapter_name="OneBot V11",
            message=_make_message(),
            reply_to_message_id="msg-1",
            image_urls=None,
            reply_context=None,
            reply_context_requested=False,
            reply_context_refetched=False,
            force_reply=True,
            reason="platform_summon",
            recall_session=fake_recall_session(handler.hindsight_memory),
            store_current=True,
        )
    )

    assert pending is None
    assert failure is not None
    assert failure.stage == "generate"
    assert failure.error_type == "RuntimeError"
    assert "callback_failed" in (failure.summary or "")
