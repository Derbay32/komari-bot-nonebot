"""Komari Chat 消息处理器测试。"""

from __future__ import annotations

import asyncio
from importlib import import_module
from types import SimpleNamespace
from typing import TYPE_CHECKING, Protocol, cast

import nonebot.plugin

from komari_bot.plugins.komari_memory.services.redis_manager import MessageSchema

if TYPE_CHECKING:
    import pytest

message_handler_module = import_module(
    "komari_bot.plugins.komari_chat.handlers.message_handler"
)


class _FakeEvent:
    def __init__(self, text: str, *, to_me: bool = False) -> None:
        self._text = text
        self.to_me = to_me

    def get_plaintext(self) -> str:
        return self._text


class _MessageHandlerLike(Protocol):
    def _resolve_trigger_message(self, event: _FakeEvent) -> tuple[bool, str]: ...


def _build_handler() -> _MessageHandlerLike:
    return cast(
        "_MessageHandlerLike",
        message_handler_module.MessageHandler.__new__(
            message_handler_module.MessageHandler
        ),
    )


def _patch_config(
    monkeypatch: pytest.MonkeyPatch,
    *,
    bot_nickname: str = "小鞠知花",
) -> None:
    monkeypatch.setattr(
        message_handler_module,
        "get_config",
        lambda: SimpleNamespace(
            bot_nickname=bot_nickname,
            bot_aliases=["小鞠", "小鞠知花", "komari"],
        ),
    )


def test_resolve_trigger_message_uses_nonebot_to_me(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = _build_handler()
    _patch_config(monkeypatch)

    at_trigger, message_content = handler._resolve_trigger_message(
        _FakeEvent("我不吃药！", to_me=True)
    )

    assert at_trigger is True
    assert message_content == "我不吃药！"


def test_resolve_trigger_message_detects_plain_text_at_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = _build_handler()
    _patch_config(monkeypatch)

    at_trigger, message_content = handler._resolve_trigger_message(
        _FakeEvent("@小鞠知花 我不吃药！")
    )

    assert at_trigger is True
    assert message_content == "我不吃药！"


def test_resolve_trigger_message_keeps_regular_text_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = _build_handler()
    _patch_config(monkeypatch)

    at_trigger, message_content = handler._resolve_trigger_message(
        _FakeEvent("我觉得小鞠知花今天会装傻。")
    )

    assert at_trigger is False
    assert message_content == "我觉得小鞠知花今天会装傻。"


class _FakeRedis:
    def __init__(self, history: list[MessageSchema]) -> None:
        self.history = list(history)
        self.pushed_messages: list[MessageSchema] = []

    async def get_buffer(self, group_id: str, limit: int = 100) -> list[MessageSchema]:
        del group_id, limit
        return list(self.history)

    async def push_message(self, group_id: str, message: MessageSchema) -> None:
        del group_id
        self.pushed_messages.append(message)
        self.history.append(message)

    async def increment_message_count(self, group_id: str) -> int:
        del group_id
        return 1

    async def increment_tokens(self, group_id: str, count: int) -> int:
        del group_id
        return count


class _FakeMemory:
    async def search_conversations(self, **_kwargs: object) -> list[dict[str, object]]:
        return []


class _FakeQueryRewrite:
    def __init__(self) -> None:
        self.history: list[MessageSchema] = []

    async def rewrite_query(
        self,
        current_query: str,
        conversation_history: list[MessageSchema],
    ) -> str:
        del current_query
        self.history = list(conversation_history)
        return "重写后的查询"


class _FakeEmbeddingProvider:
    async def embed(self, text: str) -> list[float]:
        assert text == "重写后的查询"
        return [0.1, 0.2]


def test_attempt_reply_uses_history_before_current_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_message = MessageSchema(
        user_id="user-1",
        user_nickname="阿虚",
        group_id="group-1",
        content="前一条过滤后文本",
        timestamp=1.0,
        message_id="msg-1",
    )
    current_message = MessageSchema(
        user_id="user-1",
        user_nickname="阿虚",
        group_id="group-1",
        content="当前待回复消息",
        timestamp=2.0,
        message_id="msg-2",
    )

    redis = _FakeRedis([previous_message])
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = redis
    handler.memory = _FakeMemory()
    handler.query_rewrite = _FakeQueryRewrite()

    async def _fake_build_prompt(**_kwargs: object) -> list[dict[str, object]]:
        return []

    async def _fake_generate_reply(**_kwargs: object) -> str:
        return "收到啦"

    monkeypatch.setattr(
        message_handler_module,
        "get_config",
        lambda: SimpleNamespace(
            proactive_enabled=False,
            context_messages_limit=10,
            memory_search_limit=3,
            bot_nickname="小鞠",
        ),
    )
    monkeypatch.setattr(
        message_handler_module,
        "build_prompt",
        _fake_build_prompt,
    )
    monkeypatch.setattr(message_handler_module, "generate_reply", _fake_generate_reply)
    original_require = nonebot.plugin.require

    def _fake_require(name: str) -> object:
        if name == "embedding_provider":
            return _FakeEmbeddingProvider()
        return original_require(name)

    monkeypatch.setattr(nonebot.plugin, "require", _fake_require)

    result = asyncio.run(
        handler._attempt_reply(
            message=current_message,
            reply_to_message_id=current_message.message_id,
            image_urls=None,
            force_reply=True,
            reason="at",
            reply_score=0.9,
            store_current=True,
        )
    )

    assert result[0] == {
        "reply": "收到啦",
        "reply_to_message_id": current_message.message_id,
    }
    assert [msg.content for msg in handler.query_rewrite.history] == ["前一条过滤后文本"]
