"""komari_chat 的 chat 封禁回复压制测试。"""

from __future__ import annotations

from importlib import import_module
from types import SimpleNamespace
from typing import Any

import pytest
from nonebot.adapters.onebot.v11 import Message
from nonebot.adapters.onebot.v11.event import Sender

from komari_bot.plugins.komari_chat.handlers.message_handler import MessageHandler
from komari_bot.plugins.komari_decision.services.decision_engine import DecisionOutcome

message_handler_module = import_module(
    "komari_bot.plugins.komari_chat.handlers.message_handler"
)


class _Redis:
    def __init__(self) -> None:
        self.pushed_messages: list[object] = []

    async def push_message(self, _group_id: str, message: object) -> None:
        self.pushed_messages.append(message)


class _DecisionEngine:
    async def evaluate(self, **_kwargs: object) -> DecisionOutcome:
        return DecisionOutcome(
            memory_action="store",
            should_reply=True,
            force_reply=True,
            reply_reason="at",
            forced_reply_reason="at",
            reply_score=None,
            alias_hit=None,
            call_intent="none",
            call_margin=None,
            best_scene_id=None,
            scene_score=None,
            timing_score=None,
            noise_score=None,
            meaningful_score=None,
            call_direct_score=None,
            call_mention_score=None,
            filter_reason=None,
            rank_result=None,
            timing_breakdown=None,
        )


class _Event:
    user_id = 10086
    group_id = 20000
    message_id = 30000
    self_id = 669293859
    to_me = True
    reply = None
    message = Message("@小鞠 你好")
    sender = Sender.model_construct(user_id=user_id, nickname="测试用户", card="")

    def get_plaintext(self) -> str:
        return "你好"


@pytest.mark.asyncio
async def test_chat_ban_stores_message_without_attempting_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = MessageHandler.__new__(MessageHandler)
    redis = _Redis()
    handler.redis = redis  # type: ignore[assignment]
    handler.memory = SimpleNamespace()  # type: ignore[assignment]
    handler.decision_engine = _DecisionEngine()  # type: ignore[assignment]
    decisions: list[dict[str, object]] = []

    async def fail_attempt_reply(**_kwargs: object) -> Any:
        raise AssertionError

    async def fail_reaction() -> None:
        raise AssertionError

    monkeypatch.setattr(
        message_handler_module,
        "get_config",
        lambda: SimpleNamespace(bot_nickname="小鞠", bot_aliases=[]),
    )
    monkeypatch.setattr(handler, "_attempt_reply", fail_attempt_reply)
    monkeypatch.setattr(handler, "_log_decision", decisions.append)

    result = await handler.process_message(
        SimpleNamespace(),  # type: ignore[arg-type]
        _Event(),  # type: ignore[arg-type]
        on_reply_triggered=fail_reaction,
        reply_allowed=False,
    )

    assert result is None
    assert len(redis.pushed_messages) == 1
    assert decisions[0]["reply_action"] == "blocked_by_user_ban"
