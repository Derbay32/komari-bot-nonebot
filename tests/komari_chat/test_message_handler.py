"""Komari Chat 消息处理器测试。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from importlib import import_module
from types import SimpleNamespace
from typing import TYPE_CHECKING, Protocol, cast

import nonebot.plugin
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.adapters.onebot.v11.event import Reply, Sender

from komari_bot.plugins.komari_memory.services.redis_manager import MessageSchema

if TYPE_CHECKING:
    import pytest

message_handler_module = import_module(
    "komari_bot.plugins.komari_chat.handlers.message_handler"
)
llm_service_module = import_module("komari_bot.plugins.komari_chat.services.llm_service")


class _FakeEvent:
    def __init__(
        self,
        text: str,
        *,
        to_me: bool = False,
        reply: Reply | None = None,
        self_id: int = 669293859,
    ) -> None:
        self._text = text
        self.to_me = to_me
        self.reply = reply
        self.self_id = self_id

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
        self.pushed_global_interactions: list[dict[str, object]] = []
        self.global_interaction_buffer_calls: list[dict[str, object]] = []

    async def get_buffer(self, group_id: str, limit: int = 100) -> list[MessageSchema]:
        del group_id, limit
        return list(self.history)

    async def push_message(self, group_id: str, message: MessageSchema) -> None:
        del group_id
        self.pushed_messages.append(message)
        self.history.append(message)

    async def get_global_interaction_buffer(
        self,
        user_id: str,
        limit: int = 10,
    ) -> list[dict[str, object]]:
        self.global_interaction_buffer_calls.append({"user_id": user_id, "limit": limit})
        return [{"event": "旧互动", "result": "旧回应", "emotion": "平静"}]

    async def push_global_interaction(
        self,
        *,
        user_id: str,
        record: dict[str, object],
        trigger_size: int,
    ) -> None:
        self.pushed_global_interactions.append(
            {"user_id": user_id, "record": record, "trigger_size": trigger_size}
        )


class _FakeMemory:
    def __init__(self) -> None:
        self.pg_pool = object()
        self.interaction_history: dict[str, object] | None = None
        self.upsert_interaction_history_calls: list[dict[str, object]] = []
        self.search_interaction_event_calls: list[dict[str, object]] = []
        self.get_user_profile_calls: list[dict[str, object]] = []

    async def search_conversations(self, **_kwargs: object) -> list[dict[str, object]]:
        return []

    async def search_interaction_events(
        self,
        **kwargs: object,
    ) -> list[dict[str, object]]:
        self.search_interaction_event_calls.append(dict(kwargs))
        return [{"event_summary": "长期互动事件"}]

    async def get_user_profile(
        self,
        *,
        user_id: str,
        group_id: str,
    ) -> dict[str, object]:
        self.get_user_profile_calls.append({"user_id": user_id, "group_id": group_id})
        return {
            "display_name": "阿虚",
            "traits": {"性格": {"value": "经常开玩笑", "category": "general"}},
        }

    async def get_interaction_history(
        self,
        *,
        user_id: str,
        group_id: str,
    ) -> dict[str, object] | None:
        del user_id, group_id
        return self.interaction_history

    async def upsert_interaction_history(
        self,
        *,
        user_id: str,
        group_id: str,
        interaction: dict[str, object],
    ) -> None:
        self.upsert_interaction_history_calls.append(
            {"user_id": user_id, "group_id": group_id, "interaction": interaction}
        )


@asynccontextmanager
async def _fake_memory_lock(*_args: object, **kwargs: object):
    _fake_memory_lock_calls.append(kwargs)
    yield


_fake_memory_lock_calls: list[dict[str, object]] = []


class _FakeQueryRewrite:
    def __init__(self) -> None:
        self.current_query: str | None = None

    async def rewrite_query(
        self,
        current_query: str,
    ) -> str:
        self.current_query = current_query
        return "重写后的查询"


class _FakeEmbeddingProvider:
    async def embed(self, text: str) -> list[float]:
        assert text == "重写后的查询"
        return [0.1, 0.2]


class _FakeBot:
    def __init__(self, payload: dict[str, object] | Exception | None = None) -> None:
        self.payload = payload
        self.calls: list[int] = []

    async def get_msg(self, *, message_id: int) -> dict[str, object]:
        self.calls.append(message_id)
        if isinstance(self.payload, Exception):
            raise self.payload
        if self.payload is None:
            raise RuntimeError
        return self.payload


def _build_sender(
    user_id: int,
    *,
    nickname: str = "tester",
    card: str | None = None,
) -> Sender:
    return Sender.model_construct(user_id=user_id, nickname=nickname, card=card)


def _build_reply(
    *,
    sender_user_id: int,
    message: Message,
    message_id: int = 123,
    nickname: str = "tester",
) -> Reply:
    return Reply.model_construct(
        time=1,
        message_type="group",
        message_id=message_id,
        real_id=message_id,
        sender=_build_sender(sender_user_id, nickname=nickname),
        message=message,
    )


def test_attempt_reply_only_rewrites_current_message(
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
    memory = _FakeMemory()
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = redis
    handler.memory = memory
    handler.query_rewrite = _FakeQueryRewrite()
    build_prompt_kwargs: dict[str, object] = {}
    generate_with_tools_kwargs: dict[str, object] = {}

    async def _fake_build_prompt(**_kwargs: object) -> list[dict[str, object]]:
        build_prompt_kwargs.update(_kwargs)
        return []

    async def _fake_generate_reply_with_tools(**_kwargs: object) -> object:
        generate_with_tools_kwargs.update(_kwargs)
        return llm_service_module.ReplyResult(
            content="收到啦",
            interaction_history={
                "event": "发送当前待回复消息",
                "result": "回复收到啦",
                "emotion": "平静",
            },
        )

    monkeypatch.setattr(
        message_handler_module,
        "get_config",
        lambda: SimpleNamespace(
            proactive_enabled=False,
            context_messages_limit=10,
            summary_max_buffer_size=500,
            memory_search_limit=3,
            bot_nickname="小鞠",
            memory_agent_lock_timeout_seconds=5,
            global_interaction_enabled=True,
            global_interaction_trigger_size=20,
        ),
    )
    monkeypatch.setattr(
        message_handler_module,
        "build_prompt",
        _fake_build_prompt,
    )
    monkeypatch.setattr(
        message_handler_module,
        "generate_reply_with_tools",
        _fake_generate_reply_with_tools,
    )
    monkeypatch.setattr(
        message_handler_module,
        "komari_search_plugin",
        SimpleNamespace(is_search_available=lambda: False),
    )
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
            reply_context=None,
            reply_context_requested=False,
            reply_context_refetched=False,
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
    assert handler.query_rewrite.current_query == "当前待回复消息"
    assert redis.global_interaction_buffer_calls == [{"user_id": "user-1", "limit": 10}]
    assert memory.get_user_profile_calls == [{"user_id": "user-1", "group_id": "group-1"}]
    assert memory.search_interaction_event_calls == [
        {
            "user_id": "user-1",
            "query": "重写后的查询",
            "limit": 3,
            "query_embedding": [0.1, 0.2],
        }
    ]
    assert build_prompt_kwargs["current_user_profile"] == {
        "display_name": "阿虚",
        "traits": {"性格": {"value": "经常开玩笑", "category": "general"}},
    }
    assert build_prompt_kwargs["interaction_records"] == [
        {"event": "旧互动", "result": "旧回应", "emotion": "平静"}
    ]
    assert build_prompt_kwargs["interaction_memories"] == [{"event_summary": "长期互动事件"}]
    assert llm_service_module.READ_PROFILE_TOOL in generate_with_tools_kwargs["tools"]
    assert generate_with_tools_kwargs["memory_service"] is memory
    assert generate_with_tools_kwargs["group_id"] == "group-1"
    assert generate_with_tools_kwargs["max_tool_rounds"] == 5
    pushed_record = redis.pushed_global_interactions[0]["record"]
    assert isinstance(pushed_record, dict)
    assert redis.pushed_global_interactions == [
        {
            "user_id": "user-1",
            "trigger_size": 20,
            "record": {
                "version": 1,
                "event": "发送当前待回复消息",
                "result": "回复收到啦",
                "emotion": "平静",
                "display_name": "阿虚",
                "message_id": "msg-2",
                "timestamp": pushed_record["timestamp"],
            },
        }
    ]


def test_write_interaction_history_pushes_global_redis_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    redis = _FakeRedis([])
    handler.redis = redis
    handler.memory = _FakeMemory()
    monkeypatch.setattr(
        message_handler_module,
        "get_config",
        lambda: SimpleNamespace(
            global_interaction_enabled=True,
            global_interaction_trigger_size=20,
        ),
    )

    asyncio.run(
        handler._write_interaction_history(
            message=MessageSchema(
                user_id="user-1",
                user_nickname="阿虚",
                group_id="group-1",
                content="当前待回复消息",
                timestamp=2.0,
                message_id="msg-2",
            ),
            new_record={"event": "新事件", "result": "新反应", "emotion": "新心情"},
            lock_timeout_seconds=None,
        )
    )

    assert len(redis.pushed_global_interactions) == 1
    pushed = redis.pushed_global_interactions[0]
    assert pushed["user_id"] == "user-1"
    assert pushed["trigger_size"] == 20
    record = pushed["record"]
    assert isinstance(record, dict)
    assert record["version"] == 1
    assert record["event"] == "新事件"
    assert record["result"] == "新反应"
    assert record["emotion"] == "新心情"
    assert record["display_name"] == "阿虚"
    assert record["message_id"] == "msg-2"


def test_resolve_reply_context_builds_user_side_text_context() -> None:
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    event = _FakeEvent(
        "你怎么看",
        to_me=True,
        reply=_build_reply(
            sender_user_id=42,
            nickname="阿虚",
            message=Message("她刚才提到的角色是谁？"),
        ),
    )

    result = asyncio.run(
        handler._resolve_reply_context(
            bot=_FakeBot(),
            event=event,
            at_trigger=True,
        )
    )

    assert result.refetched is False
    assert result.context is not None
    assert result.context.source_side == "user"
    assert result.context.user_id == "42"
    assert result.context.user_nickname == "阿虚"
    assert result.context.text == "她刚才提到的角色是谁？"
    assert result.context.image_count == 0
    assert result.context.has_visible_image is False


def test_resolve_reply_context_builds_assistant_side_text_context() -> None:
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    event = _FakeEvent(
        "继续说",
        to_me=True,
        self_id=669293859,
        reply=_build_reply(
            sender_user_id=669293859,
            nickname="小鞠",
            message=Message("上一条是机器人说的话"),
        ),
    )

    result = asyncio.run(
        handler._resolve_reply_context(
            bot=_FakeBot(),
            event=event,
            at_trigger=True,
        )
    )

    assert result.context is not None
    assert result.context.source_side == "assistant"
    assert result.context.text == "上一条是机器人说的话"


def test_resolve_reply_context_extracts_image_sources_from_url_and_file() -> None:
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    reply_message = Message(
        [
            MessageSegment("image", {"url": "https://example.com/a.png"}),
            MessageSegment("image", {"file": "https://example.com/b.png"}),
        ]
    )
    event = _FakeEvent(
        "看看这张图",
        to_me=True,
        reply=_build_reply(
            sender_user_id=42,
            nickname="阿虚",
            message=reply_message,
        ),
    )

    result = asyncio.run(
        handler._resolve_reply_context(
            bot=_FakeBot(),
            event=event,
            at_trigger=True,
        )
    )

    assert result.refetched is False
    assert result.context is not None
    assert result.context.image_count == 2
    assert result.context.has_visible_image is True
    assert result.context.image_sources == (
        "https://example.com/a.png",
        "https://example.com/b.png",
    )


def test_resolve_reply_context_refetches_when_image_source_is_missing() -> None:
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    event = _FakeEvent(
        "图呢",
        to_me=True,
        reply=_build_reply(
            sender_user_id=42,
            nickname="阿虚",
            message=Message([MessageSegment("image", {"file": "cache://image"})]),
            message_id=456,
        ),
    )
    bot = _FakeBot(
        {
            "time": 1,
            "message_type": "group",
            "message_id": 456,
            "real_id": 456,
            "sender": {"user_id": 42, "nickname": "阿虚"},
            "message": [
                {
                    "type": "image",
                    "data": {"url": "https://example.com/refetched.png"},
                }
            ],
        }
    )

    result = asyncio.run(
        handler._resolve_reply_context(
            bot=bot,
            event=event,
            at_trigger=True,
        )
    )

    assert bot.calls == [456]
    assert result.refetched is True
    assert result.context is not None
    assert result.context.image_count == 1
    assert result.context.image_sources == ("https://example.com/refetched.png",)
    assert result.context.has_visible_image is True


def test_resolve_reply_context_skips_when_message_is_not_to_bot() -> None:
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    bot = _FakeBot(
        {
            "time": 1,
            "message_type": "group",
            "message_id": 999,
            "real_id": 999,
            "sender": {"user_id": 42, "nickname": "阿虚"},
            "message": [{"type": "text", "data": {"text": "不会被用到"}}],
        }
    )
    event = _FakeEvent(
        "普通消息",
        to_me=False,
        reply=_build_reply(
            sender_user_id=42,
            nickname="阿虚",
            message=Message("被回复原文"),
            message_id=999,
        ),
    )

    result = asyncio.run(
        handler._resolve_reply_context(
            bot=bot,
            event=event,
            at_trigger=False,
        )
    )

    assert result.context is None
    assert result.refetched is False
    assert bot.calls == []
