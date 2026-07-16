"""Komari Chat 消息处理器测试。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from importlib import import_module
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Protocol, cast

import nonebot.plugin
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.adapters.onebot.v11.event import Reply, Sender

from komari_bot.plugins.komari_memory.services.redis_manager import MessageSchema
from komari_bot.plugins.llm_provider.diagnostic import LLMDiagnosticCollector

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
        self.request_trace_id: str | None = None
        self.parent_call_id: str | None = None
        self.collector: object = None

    async def rewrite_query(
        self,
        current_query: str,
        *,
        request_trace_id: str | None = None,
        parent_call_id: str | None = None,
        collector: object = None,
    ) -> str:
        self.current_query = current_query
        self.request_trace_id = request_trace_id
        self.parent_call_id = parent_call_id
        self.collector = collector
        return "重写后的查询"


class _FakeEmbeddingProvider:
    async def embed(self, text: str) -> list[float]:
        assert text == "重写后的查询"
        return [0.1, 0.2]


class _FakeBot:
    self_id = "669293859"

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
            favorability_delta=1,
            favorability_reason="正常互动",
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

    pending_reply = result[0]
    assert pending_reply is not None
    assert pending_reply.reply == "收到啦"
    assert pending_reply.reply_to_message_id == current_message.message_id
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
    assert llm_service_module.RECORD_FAVORABILITY_DELTA_TOOL in generate_with_tools_kwargs["tools"]
    assert generate_with_tools_kwargs["memory_service"] is memory
    assert generate_with_tools_kwargs["group_id"] == "group-1"
    assert generate_with_tools_kwargs["max_tool_rounds"] == 5
    injected_favorability = cast("SimpleNamespace", build_prompt_kwargs["favorability"])
    assert injected_favorability.favorability == 0
    assert generate_with_tools_kwargs["max_favorability_delta"] == 5
    assert redis.pushed_global_interactions == []

    asyncio.run(handler.commit_delivered_reply(pending_reply))

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


# ── debug reply 测试 ──


class _FakeRedisForDebug:
    """Debug 测试用 Redis：记录所有副作用调用。"""

    def __init__(self, history: list[MessageSchema] | None = None) -> None:
        self.history = list(history or [])
        self.pushed_messages: list[MessageSchema] = []
        self.pushed_global_interactions: list[dict[str, object]] = []
        self.global_interaction_buffer_calls: list[dict[str, object]] = []
        self.cooldown_calls: list[str] = []
        self.increment_proactive_calls: list[str] = []

    async def get_buffer(self, group_id: str, limit: int = 100) -> list[MessageSchema]:
        del group_id, limit
        return list(self.history)

    async def push_message(self, group_id: str, message: MessageSchema) -> None:
        del group_id
        self.pushed_messages.append(message)

    async def get_global_interaction_buffer(
        self, user_id: str, limit: int = 10
    ) -> list[dict[str, object]]:
        self.global_interaction_buffer_calls.append({"user_id": user_id, "limit": limit})
        return []

    async def push_global_interaction(
        self, *, user_id: str, record: dict[str, object], trigger_size: int
    ) -> None:
        self.pushed_global_interactions.append(
            {"user_id": user_id, "record": record, "trigger_size": trigger_size}
        )

    async def is_on_cooldown(self, _group_id: str) -> bool:
        return False

    async def get_proactive_count(self, _group_id: str) -> int:
        return 0

    async def set_cooldown(self, group_id: str, seconds: int) -> None:
        del seconds
        self.cooldown_calls.append(group_id)

    async def increment_proactive_count(self, group_id: str) -> None:
        self.increment_proactive_calls.append(group_id)


class _FakeMemoryForDebug:
    def __init__(self) -> None:
        self.search_conversation_calls: list[dict[str, object]] = []
        self.get_user_profile_calls: list[dict[str, object]] = []

    async def search_conversations(self, **_kwargs: object) -> list[dict[str, object]]:
        self.search_conversation_calls.append(dict(_kwargs))
        return []

    async def search_interaction_events(
        self, **_kwargs: object
    ) -> list[dict[str, object]]:
        return []

    async def get_user_profile(
        self, *, user_id: str, group_id: str
    ) -> dict[str, object] | None:
        self.get_user_profile_calls.append({"user_id": user_id, "group_id": group_id})
        return {"display_name": "test_user", "traits": {}}


class _FakeUserDataForDebug:
    """记录 adjust 调用，验证 debug 路径不调 adjust。"""

    def __init__(self) -> None:
        self.adjust_calls: list[dict[str, object]] = []
        self.favorability_calls: list[str] = []

    def get_config(self) -> SimpleNamespace:
        return SimpleNamespace(max_favorability_delta_per_reply=5)

    async def get_user_favorability(self, user_id: str) -> SimpleNamespace:
        self.favorability_calls.append(user_id)
        return SimpleNamespace(
            user_id=user_id,
            favorability=0,
            stage_index=1,
            stage_name="疏离戒备",
            stage_prompt="保持距离",
            updated_at="2026-01-01T00:00:00+00:00",
        )

    async def adjust_user_favorability(self, user_id: str, delta: int) -> SimpleNamespace:
        self.adjust_calls.append({"user_id": user_id, "delta": delta})
        return SimpleNamespace(
            user_id=user_id, before=0, delta=delta, after=delta, stage_index=1
        )


def test_generate_debug_reply_skips_all_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 debug reply 路径不触发 Redis push、好感度 adjust、互动写入、冷却/频控。"""
    redis = _FakeRedisForDebug()
    memory = _FakeMemoryForDebug()
    fake_user_data = _FakeUserDataForDebug()
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = redis
    handler.memory = memory
    handler.query_rewrite = _FakeQueryRewrite()
    monkeypatch.setattr(message_handler_module, "user_data_plugin", fake_user_data)
    monkeypatch.setattr(
        message_handler_module,
        "komari_search_plugin",
        SimpleNamespace(is_search_available=lambda: False),
    )

    # 注入必要的全局配置
    monkeypatch.setattr(
        message_handler_module,
        "get_config",
        lambda: SimpleNamespace(
            summary_max_buffer_size=500,
            memory_search_limit=3,
            bot_nickname="小鞠",
            memory_agent_lock_timeout_seconds=5,
            global_interaction_enabled=True,
            global_interaction_trigger_size=20,
            vision_tool_enabled=False,
        ),
    )

    # 注入 fake build_prompt 和 generate_reply_with_tools
    async def _fake_build_prompt(**_kwargs: object) -> list[dict[str, object]]:
        return [{"role": "user", "content": "test"}]

    async def _fake_generate(**_kwargs: object) -> object:
        return llm_service_module.ReplyResult(
            content="debug测试回复",
            interaction_history={"event": "测试", "result": "测试回复", "emotion": "平静"},
            favorability_delta=1,
            favorability_reason="debug测试",
        )

    monkeypatch.setattr(message_handler_module, "build_prompt", _fake_build_prompt)
    monkeypatch.setattr(message_handler_module, "generate_reply_with_tools", _fake_generate)
    monkeypatch.setattr(message_handler_module, "generate_reply", _fake_generate)

    original_require = nonebot.plugin.require

    def _fake_require(name: str) -> object:
        if name == "embedding_provider":
            return _FakeEmbeddingProvider()
        return original_require(name)

    monkeypatch.setattr(nonebot.plugin, "require", _fake_require)

    result = asyncio.run(
        handler.generate_debug_reply(
            group_id="debug-group",
            user_id="user-debug",
            user_nickname="测试用户",
            content="这是一条debug测试消息",
            image_urls=None,
            reply_context=None,
        )
    )

    # 断言返回结构完整
    assert result.reply == "debug测试回复"
    assert result.favorability_delta == 1
    assert result.favorability_reason == "debug测试"
    assert result.interaction_history == {
        "event": "测试",
        "result": "测试回复",
        "emotion": "平静",
    }
    assert result.collector is not None
    assert result.collector.request_id.startswith("debug-reply-")
    assert result.reply_to_message_id is None

    # 断言零副作用
    assert redis.pushed_messages == []  # 没有 push 当前消息或 AI 回复
    assert redis.pushed_global_interactions == []  # 没有写互动历史
    assert redis.cooldown_calls == []  # 没有设冷却
    assert redis.increment_proactive_calls == []  # 没有频控计数
    assert fake_user_data.adjust_calls == []  # 没有调好感度 adjust


def test_generate_debug_reply_collector_has_query_rewrite_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 debug reply 的 collector 包含查询重写和生成阶段的 trace。"""
    redis = _FakeRedisForDebug()
    memory = _FakeMemoryForDebug()
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = redis
    handler.memory = memory
    handler.query_rewrite = _FakeQueryRewrite()
    monkeypatch.setattr(message_handler_module, "user_data_plugin", _FakeUserDataForDebug())
    monkeypatch.setattr(
        message_handler_module,
        "komari_search_plugin",
        SimpleNamespace(is_search_available=lambda: False),
    )
    monkeypatch.setattr(
        message_handler_module,
        "get_config",
        lambda: SimpleNamespace(
            summary_max_buffer_size=500,
            memory_search_limit=3,
            bot_nickname="小鞠",
            memory_agent_lock_timeout_seconds=5,
            global_interaction_enabled=True,
            global_interaction_trigger_size=20,
            vision_tool_enabled=False,
        ),
    )

    async def _fake_build_prompt(**_kwargs: object) -> list[dict[str, object]]:
        return [{"role": "user", "content": "test"}]

    collector_from_generate: object = None
    trace_id_from_generate: str | None = None

    async def _fake_generate_with_tools(**kwargs: object) -> object:
        nonlocal collector_from_generate, trace_id_from_generate
        collector_from_generate = kwargs.get("collector")
        trace_id_from_generate = cast("str | None", kwargs.get("request_trace_id"))
        return llm_service_module.ReplyResult(
            content="带trace的回复",
            interaction_history={"event": "trace", "result": "trace回复", "emotion": "平静"},
            favorability_delta=2,
            favorability_reason="trace测试",
        )

    monkeypatch.setattr(message_handler_module, "build_prompt", _fake_build_prompt)
    monkeypatch.setattr(message_handler_module, "generate_reply_with_tools", _fake_generate_with_tools)
    monkeypatch.setattr(message_handler_module, "generate_reply", _fake_generate_with_tools)

    original_require = nonebot.plugin.require

    def _fake_require(name: str) -> object:
        if name == "embedding_provider":
            return _FakeEmbeddingProvider()
        return original_require(name)

    monkeypatch.setattr(nonebot.plugin, "require", _fake_require)

    supplied_collector = LLMDiagnosticCollector(request_id="debug-reply-supplied")
    result = asyncio.run(
        handler.generate_debug_reply(
            group_id="debug-group-2",
            user_id="user-trace",
            user_nickname="trace用户",
            content="debug trace测试",
            collector=supplied_collector,
        )
    )

    assert result.collector is supplied_collector
    assert result.collector.request_id == "debug-reply-supplied"
    assert collector_from_generate is result.collector
    assert handler.query_rewrite.request_trace_id == result.collector.request_id
    assert trace_id_from_generate == result.collector.request_id


def test_generate_debug_reply_with_images_and_reply_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 debug reply 附带引用消息和图片时仍正确工作且无副作用。"""
    redis = _FakeRedisForDebug()
    memory = _FakeMemoryForDebug()
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = redis
    handler.memory = memory
    handler.query_rewrite = _FakeQueryRewrite()
    monkeypatch.setattr(message_handler_module, "user_data_plugin", _FakeUserDataForDebug())
    monkeypatch.setattr(
        message_handler_module,
        "komari_search_plugin",
        SimpleNamespace(is_search_available=lambda: False),
    )
    monkeypatch.setattr(
        message_handler_module,
        "get_config",
        lambda: SimpleNamespace(
            summary_max_buffer_size=500,
            memory_search_limit=3,
            bot_nickname="小鞠",
            memory_agent_lock_timeout_seconds=5,
            global_interaction_enabled=True,
            global_interaction_trigger_size=20,
            vision_tool_enabled=False,
        ),
    )

    build_prompt_kwargs: dict[str, object] = {}

    async def _fake_build_prompt(**_kwargs: object) -> list[dict[str, object]]:
        build_prompt_kwargs.update(_kwargs)
        return [{"role": "user", "content": "test"}]

    async def _fake_generate(**_kwargs: object) -> object:
        return llm_service_module.ReplyResult(
            content="附图debug回复",
            interaction_history={"event": "附图测试", "result": "附图回复", "emotion": "平静"},
            favorability_delta=0,
            favorability_reason="无变化",
        )

    monkeypatch.setattr(message_handler_module, "build_prompt", _fake_build_prompt)
    monkeypatch.setattr(message_handler_module, "generate_reply_with_tools", _fake_generate)
    monkeypatch.setattr(message_handler_module, "generate_reply", _fake_generate)
    async def _download_images(urls: list[str]) -> list[str]:
        return [f"base64:{url}" for url in urls]

    monkeypatch.setattr(
        message_handler_module,
        "download_images_as_base64",
        _download_images,
    )

    original_require = nonebot.plugin.require

    def _fake_require(name: str) -> object:
        if name == "embedding_provider":
            return _FakeEmbeddingProvider()
        return original_require(name)

    monkeypatch.setattr(nonebot.plugin, "require", _fake_require)

    from komari_bot.plugins.komari_chat.services.reply_context import ReplyContext

    reply_ctx = ReplyContext(
        source_side="user",
        message_id="ref-msg-1",
        user_id="ref-user",
        user_nickname="引用用户",
        text="被引用的消息",
        image_sources=("https://example.com/ref.png",),
        image_count=1,
        has_visible_image=True,
    )

    result = asyncio.run(
        handler.generate_debug_reply(
            group_id="debug-group-3",
            user_id="user-img",
            user_nickname="图片用户",
            content="看看这张图是什么意思",
            image_urls=["https://example.com/img.png"],
            reply_context=reply_ctx,
        )
    )

    assert result.reply == "附图debug回复"
    assert result.collector is not None
    assert redis.pushed_messages == []
    assert redis.pushed_global_interactions == []
    assert build_prompt_kwargs.get("reply_context") is reply_ctx
    assert build_prompt_kwargs.get("image_urls") is not None


def test_normal_attempt_reply_defers_side_effects_until_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证正常回复仅在确认送达后提交副作用。"""
    current_message = MessageSchema(
        user_id="user-1",
        user_nickname="阿虚",
        group_id="group-1",
        content="当前待回复消息",
        timestamp=2.0,
        message_id="msg-normal-1",
    )
    redis = _FakeRedisForDebug()
    memory = _FakeMemoryForDebug()
    fake_user_data = _FakeUserDataForDebug()
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = redis
    handler.memory = memory
    handler.query_rewrite = _FakeQueryRewrite()
    monkeypatch.setattr(message_handler_module, "user_data_plugin", fake_user_data)
    monkeypatch.setattr(
        message_handler_module,
        "komari_search_plugin",
        SimpleNamespace(is_search_available=lambda: False),
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
            vision_tool_enabled=False,
        ),
    )

    async def _fake_build_prompt(**_kwargs: object) -> list[dict[str, object]]:
        return [{"role": "user", "content": "test"}]

    async def _fake_generate(**_kwargs: object) -> object:
        return llm_service_module.ReplyResult(
            content="正常回复",
            interaction_history={"event": "正常消息", "result": "正常回复", "emotion": "平静"},
            favorability_delta=1,
            favorability_reason="正常互动",
        )

    monkeypatch.setattr(message_handler_module, "build_prompt", _fake_build_prompt)
    monkeypatch.setattr(message_handler_module, "generate_reply_with_tools", _fake_generate)
    monkeypatch.setattr(message_handler_module, "generate_reply", _fake_generate)

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

    pending_reply = result[0]
    assert pending_reply is not None
    assert pending_reply.reply == "正常回复"
    assert pending_reply.reply_to_message_id == "msg-normal-1"
    assert result[1] is True

    # 当前用户消息属于输入缓冲，不是回复副作用；其余写入必须等待送达确认
    assert len(redis.pushed_messages) == 1
    assert fake_user_data.adjust_calls == []
    assert redis.pushed_global_interactions == []

    asyncio.run(handler.commit_delivered_reply(pending_reply))

    # 确认送达后提交回复副作用
    assert fake_user_data.adjust_calls == [{"user_id": "user-1", "delta": 1}]
    assert len(redis.pushed_messages) >= 2  # 至少：当前消息 + AI 回复
    assert len(redis.pushed_global_interactions) >= 1
    pushed_record = redis.pushed_global_interactions[0]["record"]
    assert isinstance(pushed_record, dict)
    assert pushed_record["event"] == "正常消息"
    assert pushed_record["result"] == "正常回复"
    assert pushed_record["emotion"] == "平静"


def test_normal_attempt_reply_gracefully_handles_favorability_read_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正常聊天读取好感度失败时保持旧语义：返回生成失败而非向上抛出。"""
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )

    async def _fake_read_buffers(**_kwargs: object) -> tuple[list, list, bool]:
        return [], [], True

    async def _fake_generate_core(**_kwargs: object) -> object:
        msg = "好感度服务不可用"
        raise message_handler_module._FavorabilityReadError(msg)

    monkeypatch.setattr(handler, "_read_buffers", _fake_read_buffers)
    monkeypatch.setattr(handler, "_generate_reply_core", _fake_generate_core)
    monkeypatch.setattr(
        message_handler_module,
        "get_config",
        lambda: SimpleNamespace(bot_nickname="小鞠"),
    )

    message = MessageSchema(
        user_id="user-favor-error",
        user_nickname="测试用户",
        group_id="group-favor-error",
        content="你好",
        timestamp=1.0,
        message_id="msg-favor-error",
    )
    result = asyncio.run(
        handler._attempt_reply(
            message=message,
            reply_to_message_id=message.message_id,
            image_urls=None,
            reply_context=None,
            reply_context_requested=False,
            reply_context_refetched=False,
            force_reply=True,
            reason="at",
            reply_score=1.0,
            store_current=True,
        )
    )

    assert result == (None, True)


def test_generate_debug_reply_refetches_quoted_image_without_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """引用图片缺少可下载 URL 时，debug reply 通过 get_msg 补取完整上下文。"""
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    captured: dict[str, object] = {}

    async def _fake_read_buffers(**_kwargs: object) -> tuple[list, list, bool]:
        return [], [], False

    async def _fake_generate_core(**kwargs: object) -> object:
        captured.update(kwargs)
        return llm_service_module.ReplyResult(
            content="已读取引用图片",
            interaction_history={
                "event": "引用图片",
                "result": "读取完成",
                "emotion": "平静",
            },
            favorability_delta=0,
            favorability_reason="无变化",
        )

    monkeypatch.setattr(handler, "_read_buffers", _fake_read_buffers)
    monkeypatch.setattr(handler, "_generate_reply_core", _fake_generate_core)

    from komari_bot.plugins.komari_chat.services.reply_context import ReplyContext

    original_context = ReplyContext(
        source_side="user",
        message_id="456",
        user_id="42",
        user_nickname="引用用户",
        text="图呢",
        image_sources=(),
        image_count=1,
        has_visible_image=False,
    )
    bot = _FakeBot(
        {
            "time": 1,
            "message_type": "group",
            "message_id": 456,
            "real_id": 456,
            "sender": {"user_id": 42, "nickname": "引用用户"},
            "message": [
                {
                    "type": "image",
                    "data": {"url": "https://example.com/refetched.png"},
                }
            ],
        }
    )

    result = asyncio.run(
        handler.generate_debug_reply(
            group_id="debug-group-refetch",
            user_id="42",
            user_nickname="测试用户",
            content="看看引用图片",
            _bot=cast("Any", bot),
            reply_context=original_context,
        )
    )

    refetched_context = cast("ReplyContext", captured["reply_context"])
    assert bot.calls == [456]
    assert refetched_context.image_sources == (
        "https://example.com/refetched.png",
    )
    assert captured["reply_context_refetched"] is True
    assert result.reply_to_message_id == "456"
