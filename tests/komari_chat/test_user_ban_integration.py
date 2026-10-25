"""komari_chat 的 chat 封禁回复压制测试。"""

from __future__ import annotations

from importlib import import_module
from types import SimpleNamespace
from typing import Any

import pytest
from nonebot.adapters.onebot.v11 import Message
from nonebot.adapters.onebot.v11.event import Sender

from komari_bot.decision.jev import DecisionResult
from komari_bot.plugins.komari_chat.handlers.message_handler import MessageHandler

message_handler_module = import_module(
    "komari_bot.plugins.komari_chat.handlers.message_handler"
)


_BAN_HANDOFF_FORBIDDEN_MESSAGE = "封禁时不得领取回复交接"


class _Redis:
    def __init__(self) -> None:
        self.pushed_messages: list[Any] = []

    async def push_message(self, _group_id: str, message: object) -> None:
        self.pushed_messages.append(message)


class _Dispatch:
    """本次分发的共享载体替身：required 聊天路由，交接必须不被调用。"""

    def __init__(self) -> None:
        self.request = SimpleNamespace(
            target=SimpleNamespace(text="@小鞠 你好"),
            platform_summoned=True,
        )
        self.result = DecisionResult(
            decision_id="decision-ban",
            status="success",
            reason_code="classified",
            route="chat",
            force_reply=True,
            effective_reply="required",
            retain_recent=True,
            memory_candidate=True,
        )
        self.claim_routes: list[str] = []

    async def evaluate(
        self,
        _engine: object,
        *,
        summary_candidate: bool,
    ) -> DecisionResult:
        assert summary_candidate is False
        return self.result

    async def claim_handoff(self, route: str) -> bool:
        del route
        raise AssertionError(_BAN_HANDOFF_FORBIDDEN_MESSAGE)


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
    handler.decision_engine = SimpleNamespace()  # type: ignore[assignment]
    decisions: list[dict[str, object]] = []

    async def fail_attempt_reply(**_kwargs: object) -> Any:
        raise AssertionError

    async def fail_reaction() -> None:
        raise AssertionError

    monkeypatch.setattr(
        message_handler_module,
        "get_context_config",
        lambda: SimpleNamespace(bot_nickname="小鞠", bot_aliases=[]),
    )
    monkeypatch.setattr(handler, "_attempt_reply", fail_attempt_reply)
    monkeypatch.setattr(handler, "_log_decision", decisions.append)

    dispatch = _Dispatch()
    result = await handler.process_message(
        SimpleNamespace(self_id="669293859", type="OneBot V11"),  # type: ignore[arg-type]
        _Event(),  # type: ignore[arg-type]
        on_reply_triggered=fail_reaction,
        decision_dispatch=dispatch,  # type: ignore[arg-type]
        reply_allowed=False,
    )

    assert result is None
    assert len(redis.pushed_messages) == 1
    # 分类与合格原文保存继续；原始正文（未剥离称呼）进入既有存储路径。
    assert redis.pushed_messages[0].content == "@小鞠 你好"
    assert decisions[0]["reply_action"] == "blocked_by_user_ban"
    assert decisions[0]["status"] == "success"
    assert decisions[0]["route"] == "chat"
