"""Komari Chat 消息处理器测试。"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import inspect
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Protocol, cast

from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.adapters.onebot.v11.event import Reply, Sender

from komari_bot.context.redis_store import MessageSchema
from komari_bot.plugins.komari_chat.services.dsh_chat_service import (
    ImageTransportTaskError,
)
from komari_bot.plugins.komari_chat.services.image_failure import (
    ImageFailureSummary,
    ImageUnderstandingFailureError,
)
from komari_bot.plugins.komari_chat.services.reply_context import ReplyContext

if TYPE_CHECKING:
    from collections.abc import Callable

import pytest

message_handler_module = import_module(
    "komari_bot.plugins.komari_chat.handlers.message_handler"
)
llm_service_module = import_module(
    "komari_bot.plugins.komari_chat.services.llm_service"
)
image_reading_session_module = import_module(
    "komari_bot.plugins.komari_chat.services.image_reading_session"
)
proactive_reservation_module = import_module(
    "komari_bot.plugins.komari_chat.services.proactive_reservation"
)
user_data_module = import_module("komari_bot.plugins.user_data")
ReservationDenied = proactive_reservation_module.ReservationDenied
ReservationLostError = proactive_reservation_module.ReservationLostError


def _patch_both_configs(
    monkeypatch: "pytest.MonkeyPatch",
    stub: "Callable[[], object]",
) -> None:
    """TSK-192 phase B: chat and context config seams share a duck-typed stub.

    The tested path reads chat fields through ``get_config`` and shared
    conversation policy through ``get_context_config``.
    """
    monkeypatch.setattr(message_handler_module, "get_config", stub)
    monkeypatch.setattr(message_handler_module, "get_context_config", stub)


from tests.komari_chat.hindsight_recall_support import (
    FakeHindsightMemory,
    FakeRecallSession,
    fake_recall_session,
)


def _attempt_recall_session(handler: object) -> FakeRecallSession:
    memory = getattr(handler, "hindsight_memory", None)
    if isinstance(memory, FakeHindsightMemory):
        return fake_recall_session(memory)
    return fake_recall_session(FakeHindsightMemory())



class _DebugFavorabilityWriteError(AssertionError):
    """Debug must never reach the user-data favorability write entry."""


class _FakeDshChat:
    """Capture the handler-to-DSH seam without running a model."""

    def __init__(
        self,
        *,
        content: str = "生成成功",
        delta: int | None = 1,
        reason: str | None = "测试",
        error: Exception | None = None,
        image_failure_summary: object | None = None,
    ) -> None:
        self.content = content
        self.delta = delta
        self.reason = reason
        self.error = error
        self.image_failure_summary = image_failure_summary
        self.calls: list[dict[str, Any]] = []

    async def generate(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return import_module("komari_bot.plugins.komari_chat.services.chat_result").ReplyResult(
            content=self.content,
            favorability_delta=self.delta,
            favorability_reason=self.reason,
            image_failure_summary=self.image_failure_summary,
        )

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
    _patch_both_configs(
        monkeypatch,
        lambda: SimpleNamespace(
            bot_nickname=bot_nickname,
            bot_aliases=["小鞠", "小鞠知花", "komari"],
        ),
    )


def _chat_memory_stub(**overrides: object) -> SimpleNamespace:
    """Combined chat/context config stub for handler-level tests."""
    values: dict[str, object] = {
        "proactive_enabled": False,
        "context_messages_limit": 10,
        "context_max_utf8_bytes": 24_000,
        "context_max_estimated_tokens": 6_000,
        "summary_max_buffer_size": 500,
        "memory_search_limit": 3,
        "knowledge_enabled": False,
        "knowledge_limit": 3,
        "bot_nickname": "小鞠",
        "memory_agent_lock_timeout_seconds": 5,
        "global_interaction_enabled": True,
        "global_interaction_trigger_size": 20,
        "face_reaction_enabled": False,
        "face_reaction_id": "76",
        "image_understanding_mode": "delegated",
        # TSK-194：图片下载预算 8 项字段（与配置 Schema/迁移 0015 默认值一致；
        # ImageDownloadPolicy.from_config 不再对缺字段回退隐藏默认，fixture 必须显式提供）
        "vision_image_download_max_count": 4,
        "vision_image_download_max_bytes": 8 * 1024 * 1024,
        "vision_image_download_total_max_bytes": 20 * 1024 * 1024,
        "vision_image_download_max_pixels": 40_000_000,
        "vision_image_download_concurrency": 2,
        "vision_image_download_connect_timeout_seconds": 5.0,
        "vision_image_download_read_timeout_seconds": 30.0,
        "vision_image_download_total_timeout_seconds": 45.0,
        "error_notify_enabled": False,
        "agent_max_rounds": 10,
        "agent_max_tool_calls_per_round": 4,
        "agent_max_total_tool_calls": 20,
        # TSK-193：工具调用约束模式（与配置 Schema 默认值一致；
        # from_config 不再对缺字段回退隐藏默认，fixture 必须显式提供）
        "agent_tool_call_mode": "required",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_trigger_resolution_lives_in_decision_ingress_only() -> None:
    """TSK-187：文本别名/适配器 to_me 的直通判定已移出聊天处理器。

    真实平台召唤与文本称呼的区分由判定入站投影拥有（见
    ``tests/komari_decision/test_jev_ingress.py``）；聊天处理器不得再自行
    剥离称呼、也不得把 ``to_me`` 当成召唤事实。
    """
    for removed in (
        "_resolve_trigger_message",
        "_is_at_trigger",
        "_is_reply_to_bot",
        "_strip_text_at_alias_prefix",
    ):
        assert not hasattr(message_handler_module.MessageHandler, removed), (
            f"聊天处理器不应再自行解析触发事实: {removed}"
        )


class _FakeRedis:
    def __init__(self, history: list[MessageSchema]) -> None:
        self.history = list(history)
        self.buffer_calls: list[dict[str, object]] = []
        self.pushed_messages: list[MessageSchema] = []
        self.pushed_global_interactions: list[dict[str, object]] = []
        self.global_interaction_buffer_calls: list[dict[str, object]] = []

    async def get_buffer(self, group_id: str, limit: int = 100) -> list[MessageSchema]:
        self.buffer_calls.append({"group_id": group_id, "limit": limit})
        return list(self.history[-limit:])

    async def push_message(self, group_id: str, message: MessageSchema) -> None:
        del group_id
        self.pushed_messages.append(message)
        self.history.append(message)

    async def get_global_interaction_buffer(
        self,
        user_id: str,
        limit: int = 10,
    ) -> list[dict[str, object]]:
        self.global_interaction_buffer_calls.append(
            {"user_id": user_id, "limit": limit}
        )
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


class _FakeQueryRewrite:
    def __init__(self) -> None:
        self.current_query: str | None = None
        self.request_trace_id: str | None = None

    async def rewrite_query(
        self,
        current_query: str,
        *,
        request_trace_id: str | None = None,
    ) -> str:
        self.current_query = current_query
        self.request_trace_id = request_trace_id
        return "重写后的查询"


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


def test_recent_context_uses_newest_contiguous_messages_within_budget() -> None:
    messages = [
        MessageSchema(
            user_id="u1",
            user_nickname="旧消息",
            group_id="g1",
            content="旧内容",
            timestamp=1,
            message_id="m1",
        ),
        MessageSchema(
            user_id="u2",
            user_nickname="超长消息",
            group_id="g1",
            content="长" * 1_000,
            timestamp=2,
            message_id="m2",
        ),
        MessageSchema(
            user_id="u3",
            user_nickname="最新消息",
            group_id="g1",
            content="最新内容",
            timestamp=3,
            message_id="m3",
        ),
    ]

    selected = message_handler_module.MessageHandler._select_recent_context(
        messages,
        max_messages=10,
        max_utf8_bytes=128,
        max_estimated_tokens=128,
    )

    assert [message.message_id for message in selected] == ["m3"]


def test_read_buffers_uses_context_limit_instead_of_summary_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = [
        MessageSchema(
            user_id="u1",
            user_nickname="用户",
            group_id="g1",
            content=f"消息 {index}",
            timestamp=float(index),
            message_id=f"m{index}",
        )
        for index in range(20)
    ]
    redis = _FakeRedis(history)
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = redis
    _patch_both_configs(
        monkeypatch,
        lambda: SimpleNamespace(
            context_messages_limit=5,
            context_max_utf8_bytes=24_000,
            context_max_estimated_tokens=6_000,
        ),
    )
    current = MessageSchema(
        user_id="u2",
        user_nickname="当前用户",
        group_id="g1",
        content="当前消息",
        timestamp=21,
        message_id="current",
    )

    recent, stored = asyncio.run(
        handler._read_buffers(
            group_id="g1",
            message=current,
            store_current=False,
        )
    )

    assert redis.buffer_calls == [{"group_id": "g1", "limit": 5}]
    assert [message.message_id for message in recent] == [
        "m15",
        "m16",
        "m17",
        "m18",
        "m19",
    ]
    assert stored is False


def test_read_buffers_excludes_chat_trigger_from_its_own_prompt_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chat trigger is stored after reading, so it never appears in its own context."""
    history = [
        MessageSchema(
            user_id="u1",
            user_nickname="用户",
            group_id="g1",
            content="更早的消息",
            timestamp=1.0,
            message_id="m1",
        )
    ]
    redis = _FakeRedis(history)
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = redis
    _patch_both_configs(
        monkeypatch,
        lambda: SimpleNamespace(
            context_messages_limit=10,
            context_max_utf8_bytes=24_000,
            context_max_estimated_tokens=6_000,
        ),
    )
    current = MessageSchema(
        user_id="u2",
        user_nickname="当前用户",
        group_id="g1",
        content="当前触发消息",
        timestamp=2.0,
        message_id="current",
    )

    recent, stored = asyncio.run(
        handler._read_buffers(
            group_id="g1",
            message=current,
            store_current=True,
        )
    )

    assert stored is True
    assert [message.message_id for message in recent] == ["m1"]
    assert [message.message_id for message in redis.pushed_messages] == ["current"]


def test_attempt_reply_delegates_to_dsh_service_without_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_message = MessageSchema(
        user_id="user-2",
        user_nickname="长门",
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
    memory = FakeHindsightMemory()
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = redis
    handler.hindsight_memory = memory
    service = _FakeDshChat(content="收到啦", delta=1, reason="正常互动")
    handler.dsh_chat = service
    _patch_both_configs(monkeypatch, lambda: _chat_memory_stub())

    result = asyncio.run(
        handler._attempt_reply(
            bot_self_id="bot-1",
            adapter_name="OneBot V11",
            message=current_message,
            reply_to_message_id=current_message.message_id,
            image_urls=None,
            reply_context=None,
            reply_context_requested=False,
            reply_context_refetched=False,
            force_reply=True,
            reason="platform_summon",
            recall_session=_attempt_recall_session(handler),
            store_current=True,
        )
    )

    pending_reply = result[0]
    assert pending_reply is not None
    assert pending_reply.reply == "收到啦"
    assert pending_reply.reply_to_message_id == current_message.message_id
    assert len(service.calls) == 1
    call = service.calls[0]
    assert call["message"] is current_message
    assert [message.message_id for message in call["recent_messages"]] == ["msg-1"]
    assert call["image_urls"] is None
    assert call["request_trace_id"] == "chat-msg-2"
    assert redis.global_interaction_buffer_calls == []
    assert redis.pushed_global_interactions == []

def test_message_handler_has_no_direct_side_effect_path() -> None:
    """消息生成器不拥有回复履约编排或其存储 adapter。"""
    removed_methods = (
        "_commit_side_effects",
        "_store_ai_reply",
        "_write_interaction_history",
        "prepare_pending_reply",
        "cancel_prepared_reply",
        "commit_delivered_reply",
        "retry_pending_reply_commits",
        "_reply_commit_heartbeat",
        "_mark_reply_commit_step",
        "_process_claimed_reply_commit",
        "_finish_claimed_reply_commit",
    )
    for method_name in removed_methods:
        assert not hasattr(message_handler_module.MessageHandler, method_name), (
            f"MessageHandler 不应再保留直连副作用方法 {method_name}"
        )

    signature = inspect.signature(message_handler_module.MessageHandler.__init__)
    assert "reply_commit_repository" not in signature.parameters


def test_message_handler_has_no_manual_reservation_lease_machinery() -> None:
    """AC-2 守卫：预占段无续租任务管理、无丢失事件、无所有权转移标志。

    续租节奏、丢失裁决与失败路径释放全部收编到租约 module
    （async with 退出协议结构默认完成）；handler 不得残留手工
    heartbeat / lost event / transferred 标志 / finally 条件释放。
    """
    removed_methods = (
        "_proactive_reservation_heartbeat",
        "_release_proactive_reservation",
    )
    for method_name in removed_methods:
        assert not hasattr(message_handler_module.MessageHandler, method_name), (
            f"MessageHandler 不应再保留预占手工续租/释放方法 {method_name}"
        )

    source = message_handler_module.__file__
    assert source is not None
    tree = ast.parse(Path(source).read_text(encoding="utf-8"))
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
    assert "reservation_lost" not in identifiers, "不应残留租约丢失事件标志"
    assert "reservation_transferred" not in identifiers, "不应残留所有权转移标志"


def test_generate_reply_core_has_no_dead_reason_params() -> None:
    """KOMARIBOT-11 守卫：_reason/_reply_score 死参数已删除。"""
    signature = inspect.signature(
        message_handler_module.MessageHandler._generate_reply_core
    )
    assert "_reason" not in signature.parameters, "_reason 死参数应已删除"
    assert "_reply_score" not in signature.parameters, "_reply_score 死参数应已删除"


def _wire_reaction_sent_case(
    monkeypatch: "pytest.MonkeyPatch",
    *,
    face_reaction_enabled: bool,
) -> tuple[Any, MessageSchema]:
    """reaction_sent 字段双分支公共布线（KOMARIBOT-11）。"""
    redis = _FakeRedisForDebug()
    memory = FakeHindsightMemory()
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = redis
    handler.hindsight_memory = memory
    handler._reaction_tasks = set()
    handler.dsh_chat = _FakeDshChat()

    _patch_both_configs(
        monkeypatch,
        lambda: _chat_memory_stub(
            proactive_enabled=False,
            context_messages_limit=10,
            summary_max_buffer_size=500,
            memory_search_limit=3,
            bot_nickname="小鞠",
            memory_agent_lock_timeout_seconds=5,
            global_interaction_enabled=True,
            global_interaction_trigger_size=20,
            face_reaction_enabled=face_reaction_enabled,
            face_reaction_id="76",
            image_understanding_mode="native",
            error_notify_enabled=False,
        ),
    )
    message = MessageSchema(
        user_id="user-1",
        user_nickname="测试用户",
        group_id="group-1",
        content="待回复",
        timestamp=1.0,
        message_id="msg-1",
    )
    return handler, message


def _run_reaction_sent_attempt(handler: Any, message: MessageSchema) -> Any:
    async def _dummy_reaction() -> None:
        return None

    return asyncio.run(
        handler._attempt_reply(
            bot_self_id="bot-1",
            adapter_name="OneBot V11",
            message=message,
            reply_to_message_id=message.message_id,
            image_urls=None,
            reply_context=None,
            reply_context_requested=False,
            reply_context_refetched=False,
            force_reply=True,
            reason="platform_summon",
            recall_session=_attempt_recall_session(handler),
            store_current=True,
            on_reply_triggered=_dummy_reaction,
        )
    )


def test_pending_reply_reaction_sent_true_when_reaction_dispatched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KOMARIBOT-11：表情真实派发时 PendingReply.reaction_sent=True。"""
    handler, message = _wire_reaction_sent_case(monkeypatch, face_reaction_enabled=True)
    pending_reply, stored, failure = _run_reaction_sent_attempt(handler, message)
    assert failure is None
    assert stored is True
    assert pending_reply is not None
    assert pending_reply.reaction_sent is True


def test_pending_reply_reaction_sent_false_when_reaction_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KOMARIBOT-11：face_reaction_enabled=False 边界下 reaction_sent=False。"""
    handler, message = _wire_reaction_sent_case(
        monkeypatch, face_reaction_enabled=False
    )
    pending_reply, stored, failure = _run_reaction_sent_attempt(handler, message)
    assert failure is None
    assert stored is True
    assert pending_reply is not None
    assert pending_reply.reaction_sent is False


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
            platform_summoned=True,
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
            platform_summoned=True,
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
            platform_summoned=True,
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
            platform_summoned=True,
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
            platform_summoned=False,
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

    async def get_buffer(self, group_id: str, limit: int = 100) -> list[MessageSchema]:
        del group_id, limit
        return list(self.history)

    async def push_message(self, group_id: str, message: MessageSchema) -> None:
        del group_id
        self.pushed_messages.append(message)

    async def get_global_interaction_buffer(
        self, user_id: str, limit: int = 10
    ) -> list[dict[str, object]]:
        self.global_interaction_buffer_calls.append(
            {"user_id": user_id, "limit": limit}
        )
        return []

    async def push_global_interaction(
        self, *, user_id: str, record: dict[str, object], trigger_size: int
    ) -> None:
        self.pushed_global_interactions.append(
            {"user_id": user_id, "record": record, "trigger_size": trigger_size}
        )


class _FakeReservationHandoff:
    """移交凭据 fake：冻结身份快照 + 记录调用的幂等 release()。"""

    def __init__(
        self,
        group_id: str,
        reservation_id: str,
        cooldown_seconds: int,
    ) -> None:
        self.group_id = group_id
        self.reservation_id = reservation_id
        self.cooldown_seconds = cooldown_seconds
        self.release_calls: list[str] = []

    async def release(self) -> bool:
        self.release_calls.append(self.reservation_id)
        return len(self.release_calls) == 1


class _FakeProactiveLease:
    """租约形态 fake：async with + handoff()；不建模内部续租（module 自测覆盖）。

    handoff 可注入 ReservationLostError 以驱动 handler 的丢失失败分流；
    记录 entered / exited / handed_off 结构标志（只断言协议结构，不断言
    释放等动词调用编排）。
    """

    def __init__(
        self,
        service: "_FakeProactiveReservation",
        group_id: str,
        reservation_id: str,
    ) -> None:
        self._service = service
        self.group_id = group_id
        self.reservation_id = reservation_id
        self.entered = False
        self.exited = False
        self.handed_off = False

    async def __aenter__(self) -> "_FakeProactiveLease":
        self.entered = True
        return self

    async def __aexit__(
        self,
        _exc_type: object,
        _exc: object,
        _tb: object,
    ) -> None:
        self.exited = True

    async def handoff(self) -> _FakeReservationHandoff:
        if self.handed_off:
            msg = "重复 handoff"
            raise proactive_reservation_module.ReservationStateError(msg)
        if self._service.handoff_error is not None:
            raise self._service.handoff_error
        self.handed_off = True
        return _FakeReservationHandoff(
            self.group_id,
            self.reservation_id,
            self._service.cooldown_seconds,
        )


class _FakeProactiveReservation:
    """租约形态的预占 module fake：reserve 返回租约或 ReservationDenied。

    只提供领域结果开关（denied reason / handoff 错误），不记录动词调用
    序列——handler 侧断言一律走领域结果（回复、失败信息、遥测文案、
    租约协议结构）。
    """

    def __init__(
        self,
        *,
        cooldown_seconds: int = 300,
        denied_reason: str | None = None,
        handoff_error: BaseException | None = None,
    ) -> None:
        self.cooldown_seconds = cooldown_seconds
        self.denied_reason = denied_reason
        self.handoff_error = handoff_error
        self.last_lease: _FakeProactiveLease | None = None

    async def reserve(
        self, group_id: str, reservation_id: str
    ) -> _FakeProactiveLease | ReservationDenied:
        if self.denied_reason is not None:
            return ReservationDenied(
                group_id=group_id,
                reservation_id=reservation_id,
                reason=self.denied_reason,
            )
        lease = _FakeProactiveLease(self, group_id, reservation_id)
        self.last_lease = lease
        return lease


def test_generate_debug_reply_skips_all_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 debug reply 路径不触发 Redis push、好感度 adjust、互动写入、冷却/频控。"""
    redis = _FakeRedisForDebug()
    memory = FakeHindsightMemory()
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = redis
    handler.hindsight_memory = memory
    handler.dsh_chat = _FakeDshChat(
        content="debug测试回复",
        delta=1,
        reason="debug测试",
    )

    async def _forbidden_adjust(*_args: object, **_kwargs: object) -> object:
        raise _DebugFavorabilityWriteError

    monkeypatch.setattr(
        user_data_module, "adjust_user_favorability", _forbidden_adjust
    )

    # 注入必要的全局配置
    _patch_both_configs(
        monkeypatch,
        lambda: _chat_memory_stub(
            summary_max_buffer_size=500,
            memory_search_limit=3,
            bot_nickname="小鞠",
            memory_agent_lock_timeout_seconds=5,
            global_interaction_enabled=True,
            global_interaction_trigger_size=20,
            image_understanding_mode="native",
        ),
    )

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
    assert not hasattr(result, "interaction_history")
    assert not hasattr(result, "collector")
    assert result.request_id.startswith("debug-reply-")
    assert result.image_failure_summary is None
    assert result.reply_to_message_id is None

    # 断言零副作用
    assert redis.pushed_messages == []  # 没有 push 当前消息或 AI 回复
    assert redis.pushed_global_interactions == []  # 没有写互动历史
    # debug 路径不触达预占 module（冷却/频控零副作用）：本测试未布线
    # proactive_reservation，任何预占触达都会以 AttributeError 红灯
    assert memory.recall_calls == []  # fake service owns recall in this seam test



def test_generate_debug_reply_projects_partial_image_summary_without_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful debug keeps only the safe summary and performs no effect."""
    redis = _FakeRedisForDebug()
    memory = FakeHindsightMemory()
    summary = ImageFailureSummary(
        mode="native",
        all_images_unavailable=False,
        total_images=2,
        attempted_images=2,
        failed_images=1,
        error_types=("image_unavailable",),
        stages=("download",),
    )
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = redis
    handler.hindsight_memory = memory
    handler.dsh_chat = _FakeDshChat(
        content="debug附图回复",
        delta=0,
        reason="无变化",
        image_failure_summary=summary,
    )
    _patch_both_configs(monkeypatch, lambda: _chat_memory_stub())

    async def _forbidden_adjust(*_args: object, **_kwargs: object) -> object:
        raise _DebugFavorabilityWriteError

    monkeypatch.setattr(
        user_data_module, "adjust_user_favorability", _forbidden_adjust
    )

    def _forbidden_notifier(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("debug must not construct the notifier")  # noqa: TRY003

    monkeypatch.setattr(
        message_handler_module, "GroupTaskFailureNotifier", _forbidden_notifier
    )

    async def _forbidden_notify(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("debug must not notify")  # noqa: TRY003

    monkeypatch.setattr(handler, "_notify_image_failure_summary", _forbidden_notify)

    async def _forbidden_summary_adjust(
        *_args: object, **_kwargs: object
    ) -> object:
        raise _DebugFavorabilityWriteError

    monkeypatch.setattr(
        user_data_module, "adjust_user_favorability", _forbidden_summary_adjust
    )

    result = asyncio.run(
        handler.generate_debug_reply(
            group_id="debug-group-summary",
            user_id="user-debug-summary",
            user_nickname="图片用户",
            content="debug 成功摘要",
        )
    )

    assert result.reply == "debug附图回复"
    assert result.image_failure_summary is summary
    rendered = dataclasses.asdict(summary)
    assert "image_unavailable" in str(rendered)
    assert "http" not in str(rendered)
    assert "base64" not in str(rendered)
    assert redis.pushed_messages == []
    assert redis.pushed_global_interactions == []
    assert memory.retain_calls == []

def test_generate_debug_reply_uses_explicit_request_trace_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TSK-200/TSK-203: the command trace ID reaches the same DSH service."""
    redis = _FakeRedisForDebug()
    memory = FakeHindsightMemory()
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = redis
    handler.hindsight_memory = memory
    service = _FakeDshChat(content="带trace的回复", delta=2, reason="trace测试")
    handler.dsh_chat = service
    _patch_both_configs(monkeypatch, lambda: _chat_memory_stub())

    result = asyncio.run(
        handler.generate_debug_reply(
            group_id="debug-group-2",
            user_id="user-trace",
            user_nickname="trace用户",
            content="debug trace测试",
            request_trace_id="debug-reply-supplied",
        )
    )

    assert result.request_id == "debug-reply-supplied"
    assert service.calls[0]["request_trace_id"] == "debug-reply-supplied"
    assert result.reply == "带trace的回复"

class _DebugImageService:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def generate(self, **kwargs: object) -> object:
        self.calls.append(dict(kwargs))
        raise self.error


def _debug_handler_with_service(
    monkeypatch: pytest.MonkeyPatch,
    service: _DebugImageService,
) -> Any:
    redis = _FakeRedisForDebug()
    memory = FakeHindsightMemory()
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = redis
    handler.hindsight_memory = memory
    handler._reaction_tasks = set()
    handler.dsh_chat = service
    _patch_both_configs(monkeypatch, lambda: _chat_memory_stub())
    return handler, redis, memory


def test_generate_debug_reply_transport_failure_has_no_business_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Debug surfaces a safe transport failure without falling back or writing."""
    redis = _FakeRedisForDebug()
    memory = FakeHindsightMemory()
    service = _DebugImageService(ImageTransportTaskError("invalid_image_input"))
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = redis
    handler.hindsight_memory = memory
    handler._reaction_tasks = set()
    handler.dsh_chat = service
    _patch_both_configs(monkeypatch, lambda: _chat_memory_stub())

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

    with pytest.raises(ImageTransportTaskError) as exc_info:
        asyncio.run(
            handler.generate_debug_reply(
                group_id="debug-group-3",
                user_id="user-img",
                user_nickname="图片用户",
                content="看看这张图是什么意思",
                image_urls=["https://example.com/img.png"],
                reply_context=reply_ctx,
            )
        )

    assert exc_info.value.code == "invalid_image_input"
    assert "example.com" not in str(exc_info.value)
    assert memory.open_calls == 1
    assert redis.pushed_messages == []
    assert redis.pushed_global_interactions == []
    assert service.calls and service.calls[0]["image_urls"] == ["https://example.com/img.png"]


def test_generate_debug_reply_image_failure_never_notifies_or_returns_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Debug image failures never construct a notifier or return a partial result."""
    error = ImageUnderstandingFailureError(
        ImageFailureSummary(
            mode="native",
            all_images_unavailable=True,
            total_images=2,
            attempted_images=2,
            failed_images=2,
            error_types=("image_unavailable",),
            stages=("download",),
        )
    )
    service = _DebugImageService(error)
    handler, redis, _memory = _debug_handler_with_service(monkeypatch, service)

    class _RecordingBot:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []

        async def call_api(self, api: str, **kwargs: object) -> object:
            self.calls.append((api, kwargs))
            return {"message_id": 1}

        async def send_private_msg(self, **kwargs: object) -> None:
            self.calls.append(("send_private_msg", kwargs))

    bot = _RecordingBot()

    def _forbidden_notifier(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("debug 路径禁止构造通知器")  # noqa: TRY003

    monkeypatch.setattr(
        message_handler_module, "GroupTaskFailureNotifier", _forbidden_notifier
    )

    async def _forbidden_notify(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("debug 路径禁止调用汇总")  # noqa: TRY003

    monkeypatch.setattr(handler, "_notify_image_failure_summary", _forbidden_notify)

    with pytest.raises(ImageUnderstandingFailureError):
        asyncio.run(
            handler.generate_debug_reply(
                group_id="debug-group-img-fail",
                user_id="user-debug-img",
                user_nickname="图片用户",
                content="看图",
                image_urls=["https://example.com/a.png", "https://example.com/b.png"],
                reply_context=None,
                _bot=bot,  # type: ignore[arg-type]
            )
        )

    assert bot.calls == []
    assert service.calls
    assert redis.pushed_messages == []

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
    memory = FakeHindsightMemory()
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = redis
    handler.hindsight_memory = memory
    handler.dsh_chat = _FakeDshChat(content="正常回复", delta=1, reason="正常互动")
    _patch_both_configs(monkeypatch, lambda: _chat_memory_stub())

    result = asyncio.run(
        handler._attempt_reply(
            bot_self_id="bot-1",
            adapter_name="OneBot V11",
            message=current_message,
            reply_to_message_id=current_message.message_id,
            image_urls=None,
            reply_context=None,
            reply_context_requested=False,
            reply_context_refetched=False,
            force_reply=True,
            reason="platform_summon",
            recall_session=_attempt_recall_session(handler),
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
    assert redis.pushed_global_interactions == []

def test_proactive_attempt_returns_pending_reply_carrying_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主动回复生成前预占；送达前只返回携带移交凭据的待履约回复。

    断言领域结果（回复内容、凭据冻结快照、租约协议结构），不断言
    reserve/renew/release/confirm 动词调用编排。
    """
    redis = _FakeRedisForDebug()
    reservation_svc = _FakeProactiveReservation(cooldown_seconds=300)
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = redis
    handler.proactive_reservation = reservation_svc

    async def _fake_read_buffers(**_kwargs: object) -> tuple[list, bool]:
        return [], True

    async def _fake_generate_core(**_kwargs: object) -> object:
        return import_module("komari_bot.plugins.komari_chat.services.chat_result").ReplyResult(
            content="主动回复",
            favorability_delta=0,
            favorability_reason="主动关心",
            hindsight_target_fingerprint="a" * 64,
        )

    monkeypatch.setattr(handler, "_read_buffers", _fake_read_buffers)
    monkeypatch.setattr(handler, "_generate_reply_core", _fake_generate_core)
    _patch_both_configs(
        monkeypatch,
        lambda: SimpleNamespace(
            proactive_enabled=True,
            proactive_max_per_hour=3,
            proactive_reservation_ttl_seconds=360,
            proactive_cooldown=300,
            bot_nickname="小鞠",
        ),
    )
    message = MessageSchema(
        user_id="user-proactive",
        user_nickname="测试用户",
        group_id="group-proactive",
        content="看起来值得主动回复",
        timestamp=1.0,
        message_id="message-proactive",
    )

    pending_reply, stored, failure = asyncio.run(
        handler._attempt_reply(
            bot_self_id="bot-1",
            adapter_name="OneBot V11",
            message=message,
            reply_to_message_id=message.message_id,
            image_urls=None,
            reply_context=None,
            reply_context_requested=False,
            reply_context_refetched=False,
            force_reply=False,
            reason="score",
            recall_session=_attempt_recall_session(handler),
            store_current=True,
        )
    )

    assert stored is True
    assert failure is None
    assert pending_reply is not None
    assert pending_reply.reply == "主动回复"
    assert pending_reply.proactive_reservation_id == "message-proactive"
    # 新形态：PendingReply 携带移交凭据（reserve 冻结快照），不再持活租约句柄
    handoff = pending_reply.proactive_handoff
    assert handoff is not None
    assert handoff.group_id == "group-proactive"
    assert handoff.reservation_id == "message-proactive"
    assert handoff.cooldown_seconds == 300
    assert not hasattr(pending_reply, "proactive_reservation")
    # 租约经 async with 激活并 handoff 移交（协议结构，不断言动词调用编排）
    lease = reservation_svc.last_lease
    assert lease is not None
    assert lease.entered is True
    assert lease.handed_off is True


def test_proactive_generation_failure_returns_failure_and_lease_exit_releases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主动回复生成失败：失败路径未移交，预占释放由租约退出协议结构默认完成。"""
    redis = _FakeRedisForDebug()
    reservation_svc = _FakeProactiveReservation()
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = redis
    handler.proactive_reservation = reservation_svc

    async def _fake_read_buffers(**_kwargs: object) -> tuple[list, bool]:
        return [], True

    async def _fail_generate_core(**_kwargs: object) -> object:
        msg = "好感度读取失败"
        raise message_handler_module._FavorabilityReadError(msg)

    monkeypatch.setattr(handler, "_read_buffers", _fake_read_buffers)
    monkeypatch.setattr(handler, "_generate_reply_core", _fail_generate_core)
    _patch_both_configs(
        monkeypatch,
        lambda: SimpleNamespace(
            proactive_enabled=True,
            proactive_max_per_hour=3,
            proactive_reservation_ttl_seconds=360,
            bot_nickname="小鞠",
        ),
    )
    message = MessageSchema(
        user_id="user-proactive",
        user_nickname="测试用户",
        group_id="group-proactive",
        content="生成会失败",
        timestamp=1.0,
        message_id="message-failed",
    )

    result = asyncio.run(
        handler._attempt_reply(
            bot_self_id="bot-1",
            adapter_name="OneBot V11",
            message=message,
            reply_to_message_id=message.message_id,
            image_urls=None,
            reply_context=None,
            reply_context_requested=False,
            reply_context_refetched=False,
            force_reply=False,
            reason="score",
            recall_session=_attempt_recall_session(handler),
            store_current=True,
        )
    )

    assert result[0] is None
    assert result[1] is True
    failure = result[2]
    assert failure is not None
    assert failure.stage == "generate"
    assert failure.error_type == "_FavorabilityReadError"
    assert failure.reaction_sent is False
    # 失败路径未产生移交凭据：租约被 async with 正常退出，
    # 释放由退出协议结构默认完成（无 finally 条件释放）
    lease = reservation_svc.last_lease
    assert lease is not None
    assert lease.entered is True
    assert lease.handed_off is False
    assert lease.exited is True


def test_normal_attempt_reply_gracefully_handles_favorability_read_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正常聊天读取好感度失败时保持旧语义：返回生成失败而非向上抛出。"""
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )

    async def _fake_read_buffers(**_kwargs: object) -> tuple[list, bool]:
        return [], True

    async def _fake_generate_core(**_kwargs: object) -> object:
        msg = "好感度服务不可用"
        raise message_handler_module._FavorabilityReadError(msg)

    monkeypatch.setattr(handler, "_read_buffers", _fake_read_buffers)
    monkeypatch.setattr(handler, "_generate_reply_core", _fake_generate_core)
    _patch_both_configs(
        monkeypatch,
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
            bot_self_id="bot-1",
            adapter_name="OneBot V11",
            message=message,
            reply_to_message_id=message.message_id,
            image_urls=None,
            reply_context=None,
            reply_context_requested=False,
            reply_context_refetched=False,
            force_reply=True,
            reason="platform_summon",
            recall_session=_attempt_recall_session(handler),
            store_current=True,
        )
    )

    assert result[0] is None
    assert result[1] is True
    failure = result[2]
    assert failure is not None
    assert failure.stage == "generate"
    assert failure.error_type == "_FavorabilityReadError"
    assert failure.reaction_sent is False


def test_generate_debug_reply_refetches_quoted_image_without_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """引用图片缺少可下载 URL 时，debug reply 通过 get_msg 补取完整上下文。"""
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    captured: dict[str, object] = {}

    async def _fake_read_buffers(**_kwargs: object) -> tuple[list, bool]:
        return [], False

    async def _fake_generate_core(**kwargs: object) -> object:
        captured.update(kwargs)
        return import_module("komari_bot.plugins.komari_chat.services.chat_result").ReplyResult(
            content="已读取引用图片",
            favorability_delta=0,
            favorability_reason="无变化",
            hindsight_target_fingerprint="a" * 64,
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
    assert refetched_context.image_sources == ("https://example.com/refetched.png",)
    assert captured["reply_context_refetched"] is True
    assert result.reply_to_message_id == "456"


# ── 表情反应前置 + 失败分流 测试 ──


def test_reaction_scheduled_before_generate_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证表情反应回调在 _generate_reply_core 之前通过 create_task 派发。"""
    reaction_called = False

    redis = _FakeRedisForDebug()
    memory = FakeHindsightMemory()
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = redis
    handler.hindsight_memory = memory
    handler.dsh_chat = _FakeDshChat(content="带表情的回复", delta=1, reason="表情互动")
    handler._reaction_tasks = set()
    handler.query_rewrite = _FakeQueryRewrite()

    _patch_both_configs(
        monkeypatch,
        lambda: _chat_memory_stub(
            proactive_enabled=False,
            context_messages_limit=10,
            summary_max_buffer_size=500,
            memory_search_limit=3,
            bot_nickname="小鞠",
            memory_agent_lock_timeout_seconds=5,
            global_interaction_enabled=True,
            global_interaction_trigger_size=20,
            face_reaction_enabled=True,
            face_reaction_id="76",
            image_understanding_mode="native",
            error_notify_enabled=False,
        ),
    )

    async def _fake_build_prompt(**_kwargs: object) -> list[dict[str, object]]:
        return [{"role": "user", "content": "test"}]

    async def _fake_generate(**_kwargs: object) -> object:
        return import_module("komari_bot.plugins.komari_chat.services.chat_result").ReplyResult(
            content="带表情的回复",
            favorability_delta=1,
            favorability_reason="表情互动",
            hindsight_target_fingerprint="a" * 64,
        )

    async def _fake_reaction() -> None:
        nonlocal reaction_called
        reaction_called = True



    current_message = MessageSchema(
        user_id="user-reaction",
        user_nickname="表情测试用户",
        group_id="group-reaction",
        content="表情测试",
        timestamp=1.0,
        message_id="msg-reaction",
    )

    result = asyncio.run(
        handler._attempt_reply(
            bot_self_id="bot-1",
            adapter_name="OneBot V11",
            message=current_message,
            reply_to_message_id=current_message.message_id,
            image_urls=None,
            reply_context=None,
            reply_context_requested=False,
            reply_context_refetched=False,
            force_reply=True,
            reason="platform_summon",
            recall_session=_attempt_recall_session(handler),
            store_current=True,
            on_reply_triggered=_fake_reaction,
        )
    )

    pending_reply, _stored, failure = result
    assert pending_reply is not None
    assert failure is None
    # 表情回调在 _schedule_reply_reaction 中被 create_task 派发
    assert reaction_called is True


def test_reaction_not_scheduled_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """face_reaction_enabled=false 时不派发表情，但仍正常生成回复。"""
    reaction_called = False
    redis = _FakeRedisForDebug()
    memory = FakeHindsightMemory()
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = redis
    handler.hindsight_memory = memory
    handler.dsh_chat = _FakeDshChat(content="无表情回复", delta=1, reason="测试")
    handler.query_rewrite = _FakeQueryRewrite()

    _patch_both_configs(
        monkeypatch,
        lambda: _chat_memory_stub(
            proactive_enabled=False,
            context_messages_limit=10,
            summary_max_buffer_size=500,
            memory_search_limit=3,
            bot_nickname="小鞠",
            memory_agent_lock_timeout_seconds=5,
            global_interaction_enabled=True,
            global_interaction_trigger_size=20,
            face_reaction_enabled=False,
            face_reaction_id="76",
            image_understanding_mode="native",
            error_notify_enabled=False,
        ),
    )

    async def _fake_build_prompt(**_kwargs: object) -> list[dict[str, object]]:
        return [{"role": "user", "content": "test"}]

    async def _fake_generate(**_kwargs: object) -> object:
        return import_module("komari_bot.plugins.komari_chat.services.chat_result").ReplyResult(
            content="无表情回复",
            favorability_delta=1,
            favorability_reason="测试",
            hindsight_target_fingerprint="a" * 64,
        )

    async def _fake_reaction() -> None:
        nonlocal reaction_called
        reaction_called = True



    current_message = MessageSchema(
        user_id="user-no-reaction",
        user_nickname="无表情用户",
        group_id="group-no-reaction",
        content="无表情",
        timestamp=1.0,
        message_id="msg-no-reaction",
    )

    result = asyncio.run(
        handler._attempt_reply(
            bot_self_id="bot-1",
            adapter_name="OneBot V11",
            message=current_message,
            reply_to_message_id=current_message.message_id,
            image_urls=None,
            reply_context=None,
            reply_context_requested=False,
            reply_context_refetched=False,
            force_reply=True,
            reason="platform_summon",
            recall_session=_attempt_recall_session(handler),
            store_current=True,
            on_reply_triggered=_fake_reaction,
        )
    )

    pending_reply, _stored, failure = result
    assert pending_reply is not None
    assert failure is None
    assert reaction_called is False


def test_reaction_sent_then_empty_reply_returns_failure_with_reaction_sent_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """贴表情后LLM返回空回复→ failure.reaction_sent=True（需发群内错误文本）。"""
    redis = _FakeRedisForDebug()
    memory = FakeHindsightMemory()
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = redis
    handler.hindsight_memory = memory
    handler.dsh_chat = _FakeDshChat(content="", delta=1, reason="测试")
    handler._reaction_tasks = set()
    handler.query_rewrite = _FakeQueryRewrite()

    _patch_both_configs(
        monkeypatch,
        lambda: _chat_memory_stub(
            proactive_enabled=False,
            context_messages_limit=10,
            summary_max_buffer_size=500,
            memory_search_limit=3,
            bot_nickname="小鞠",
            memory_agent_lock_timeout_seconds=5,
            global_interaction_enabled=True,
            global_interaction_trigger_size=20,
            face_reaction_enabled=True,
            face_reaction_id="76",
            image_understanding_mode="native",
            error_notify_enabled=False,
        ),
    )

    async def _fake_build_prompt(**_kwargs: object) -> list[dict[str, object]]:
        return [{"role": "user", "content": "test"}]

    async def _fake_generate(**_kwargs: object) -> object:
        return import_module("komari_bot.plugins.komari_chat.services.chat_result").ReplyResult(
            content="",  # 空回复
            favorability_delta=1,
            favorability_reason="测试",
            hindsight_target_fingerprint="a" * 64,
        )



    current_message = MessageSchema(
        user_id="user-empty",
        user_nickname="空回复用户",
        group_id="group-empty",
        content="空",
        timestamp=1.0,
        message_id="msg-empty",
    )

    async def _dummy_reaction_empty() -> None:
        pass

    _pending, _stored, failure = asyncio.run(
        handler._attempt_reply(
            bot_self_id="bot-1",
            adapter_name="OneBot V11",
            message=current_message,
            reply_to_message_id=current_message.message_id,
            image_urls=None,
            reply_context=None,
            reply_context_requested=False,
            reply_context_refetched=False,
            force_reply=True,
            reason="platform_summon",
            recall_session=_attempt_recall_session(handler),
            store_current=True,
            on_reply_triggered=_dummy_reaction_empty,
        )
    )

    assert failure is not None
    assert failure.error_type == "EmptyReplyError"
    assert failure.reaction_sent is True


def test_reaction_sent_then_delta_missing_returns_failure_with_reaction_sent_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """贴表情后好感度delta缺失→ failure.reaction_sent=True。"""
    redis = _FakeRedisForDebug()
    memory = FakeHindsightMemory()
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = redis
    handler.hindsight_memory = memory
    handler.dsh_chat = _FakeDshChat(content="有回复但无delta", delta=None, reason=None)
    handler._reaction_tasks = set()
    handler.query_rewrite = _FakeQueryRewrite()

    _patch_both_configs(
        monkeypatch,
        lambda: _chat_memory_stub(
            proactive_enabled=False,
            context_messages_limit=10,
            summary_max_buffer_size=500,
            memory_search_limit=3,
            bot_nickname="小鞠",
            memory_agent_lock_timeout_seconds=5,
            global_interaction_enabled=True,
            global_interaction_trigger_size=20,
            face_reaction_enabled=True,
            face_reaction_id="76",
            image_understanding_mode="native",
            error_notify_enabled=False,
        ),
    )

    async def _fake_build_prompt(**_kwargs: object) -> list[dict[str, object]]:
        return [{"role": "user", "content": "test"}]

    async def _fake_generate(**_kwargs: object) -> object:
        return import_module("komari_bot.plugins.komari_chat.services.chat_result").ReplyResult(
            content="有回复但无delta",
            favorability_delta=None,  # 缺失
            favorability_reason=None,
            hindsight_target_fingerprint="a" * 64,
        )



    current_message = MessageSchema(
        user_id="user-delta",
        user_nickname="delta用户",
        group_id="group-delta",
        content="delta",
        timestamp=1.0,
        message_id="msg-delta",
    )

    async def _dummy_reaction_delta() -> None:
        pass

    _pending, _stored, failure = asyncio.run(
        handler._attempt_reply(
            bot_self_id="bot-1",
            adapter_name="OneBot V11",
            message=current_message,
            reply_to_message_id=current_message.message_id,
            image_urls=None,
            reply_context=None,
            reply_context_requested=False,
            reply_context_refetched=False,
            force_reply=True,
            reason="platform_summon",
            recall_session=_attempt_recall_session(handler),
            store_current=True,
            on_reply_triggered=_dummy_reaction_delta,
        )
    )

    assert failure is not None
    assert failure.error_type == "FavorabilityDeltaMissingError"
    assert failure.reaction_sent is True


def test_reserve_failure_returns_failure_with_reaction_sent_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reserve阶段Redis异常→ failure.reaction_sent=False（表情尚未派发，不补发群内错误文本）。"""
    redis = _FakeRedisForDebug()
    reservation_svc = _FakeProactiveReservation()
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = redis
    handler.hindsight_memory = FakeHindsightMemory()
    handler.proactive_reservation = reservation_svc

    _patch_both_configs(
        monkeypatch,
        lambda: SimpleNamespace(
            proactive_enabled=True,
            proactive_max_per_hour=3,
            proactive_reservation_ttl_seconds=360,
            bot_nickname="小鞠",
            face_reaction_enabled=True,
            face_reaction_id="76",
            error_notify_enabled=False,
        ),
    )

    # 注入 reserve 使其抛出异常
    async def _failing_reserve(*_args: object, **_kwargs: object) -> str:
        del _args, _kwargs
        msg = "Redis 连接断开"
        raise RuntimeError(msg)

    reservation_svc.reserve = _failing_reserve  # type: ignore[method-assign]

    reaction_called = False

    async def _fake_reaction() -> None:
        nonlocal reaction_called
        reaction_called = True

    current_message = MessageSchema(
        user_id="user-reserve",
        user_nickname="reserve用户",
        group_id="group-reserve",
        content="reserve",
        timestamp=1.0,
        message_id="msg-reserve",
    )

    _pending, _stored, failure = asyncio.run(
        handler._attempt_reply(
            bot_self_id="bot-1",
            adapter_name="OneBot V11",
            message=current_message,
            reply_to_message_id=current_message.message_id,
            image_urls=None,
            reply_context=None,
            reply_context_requested=False,
            reply_context_refetched=False,
            force_reply=False,
            reason="score",
            recall_session=_attempt_recall_session(handler),
            store_current=True,
            on_reply_triggered=_fake_reaction,
        )
    )

    assert failure is not None
    assert failure.stage == "reserve"
    assert failure.error_type == "RuntimeError"
    assert failure.reaction_sent is False
    assert reaction_called is False  # 还未到贴表情阶段


class _RecordingLogger:
    """记录 debug 文案的日志替身（遥测文案语义断言，不关心输出通道）。"""

    def __init__(self) -> None:
        self.debug_messages: list[str] = []

    def debug(self, message: str, *_args: object, **_kwargs: object) -> None:
        self.debug_messages.append(message)

    def info(self, _message: str, *_args: object, **_kwargs: object) -> None:
        return None

    def warning(self, _message: str, *_args: object, **_kwargs: object) -> None:
        return None

    def exception(self, _message: str, *_args: object, **_kwargs: object) -> None:
        return None


@pytest.mark.parametrize(
    ("denied_reason", "expected_log"),
    [
        ("cooldown", "[KomariChat] 主动回复冷却或生成预占中"),
        ("rate_limited", "[KomariChat] 主动回复频率超限"),
        ("duplicate", "[KomariChat] 主动回复消息已预占或已送达"),
    ],
)
def test_proactive_denied_reason_records_branch_telemetry_and_returns_no_reply(
    monkeypatch: pytest.MonkeyPatch,
    denied_reason: str,
    expected_log: str,
) -> None:
    """ReservationDenied 按 reason 三分支记录遥测文案（原文案语义），正常控制流返回。"""
    redis = _FakeRedisForDebug()
    fake_logger = _RecordingLogger()
    monkeypatch.setattr(message_handler_module, "logger", fake_logger)
    reservation_svc = _FakeProactiveReservation(denied_reason=denied_reason)
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = redis
    handler.proactive_reservation = reservation_svc
    _patch_both_configs(
        monkeypatch,
        lambda: SimpleNamespace(
            proactive_enabled=True,
            proactive_max_per_hour=3,
            proactive_reservation_ttl_seconds=360,
            proactive_cooldown=300,
            bot_nickname="小鞠",
        ),
    )
    message = MessageSchema(
        user_id="user-denied",
        user_nickname="拒绝用户",
        group_id="group-denied",
        content="频控消息",
        timestamp=1.0,
        message_id="message-denied",
    )

    pending_reply, stored, failure = asyncio.run(
        handler._attempt_reply(
            bot_self_id="bot-1",
            adapter_name="OneBot V11",
            message=message,
            reply_to_message_id=message.message_id,
            image_urls=None,
            reply_context=None,
            reply_context_requested=False,
            reply_context_refetched=False,
            force_reply=False,
            reason="score",
            recall_session=_attempt_recall_session(handler),
            store_current=True,
        )
    )

    assert pending_reply is None
    assert stored is False
    assert failure is None
    assert fake_logger.debug_messages == [expected_log]


def test_proactive_handoff_lost_returns_generate_failure_with_reaction_sent_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ReservationLostError → ReplyFailureInfo(stage="generate", error_type="ProactiveReservationLostError")。

    生成完成、移交时租约已丢失：表情已派发，failure.reaction_sent=True。
    """
    redis = _FakeRedisForDebug()
    memory = FakeHindsightMemory()
    reservation_svc = _FakeProactiveReservation(
        handoff_error=ReservationLostError(
            group_id="group-lost",
            reservation_id="message-lost",
        )
    )
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = redis
    handler.hindsight_memory = memory
    handler._reaction_tasks = set()
    handler.proactive_reservation = reservation_svc

    async def _fake_read_buffers(**_kwargs: object) -> tuple[list, bool]:
        return [], True

    async def _fake_generate_core(**_kwargs: object) -> object:
        return import_module("komari_bot.plugins.komari_chat.services.chat_result").ReplyResult(
            content="生成成功但租约已丢",
            favorability_delta=1,
            favorability_reason="测试",
            hindsight_target_fingerprint="a" * 64,
        )

    monkeypatch.setattr(handler, "_read_buffers", _fake_read_buffers)
    monkeypatch.setattr(handler, "_generate_reply_core", _fake_generate_core)
    _patch_both_configs(
        monkeypatch,
        lambda: SimpleNamespace(
            proactive_enabled=True,
            proactive_max_per_hour=3,
            proactive_reservation_ttl_seconds=360,
            proactive_cooldown=300,
            bot_nickname="小鞠",
            face_reaction_enabled=True,
            face_reaction_id="76",
            error_notify_enabled=False,
        ),
    )

    reaction_called = False

    async def _fake_reaction() -> None:
        nonlocal reaction_called
        reaction_called = True

    message = MessageSchema(
        user_id="user-lost",
        user_nickname="丢失用户",
        group_id="group-lost",
        content="租约丢失消息",
        timestamp=1.0,
        message_id="message-lost",
    )

    pending_reply, stored, failure = asyncio.run(
        handler._attempt_reply(
            bot_self_id="bot-1",
            adapter_name="OneBot V11",
            message=message,
            reply_to_message_id=message.message_id,
            image_urls=None,
            reply_context=None,
            reply_context_requested=False,
            reply_context_refetched=False,
            force_reply=False,
            reason="score",
            recall_session=_attempt_recall_session(handler),
            store_current=True,
            on_reply_triggered=_fake_reaction,
        )
    )

    assert pending_reply is None
    assert stored is True
    assert failure is not None
    assert failure.stage == "generate"
    assert failure.error_type == "ProactiveReservationLostError"
    assert failure.reaction_sent is True
    assert reaction_called is True
    # 丢失路径未移交：租约退出协议承担释放
    lease = reservation_svc.last_lease
    assert lease is not None
    assert lease.handed_off is False
    assert lease.exited is True


def test_pending_reply_does_not_retain_reaction_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """表情回调只在生成前派发，不进入冻结的待履约回复。"""
    redis = _FakeRedisForDebug()
    memory = FakeHindsightMemory()
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = redis
    handler.hindsight_memory = memory
    handler.dsh_chat = _FakeDshChat(content="送达测试", delta=1, reason="测试")
    handler.query_rewrite = _FakeQueryRewrite()

    _patch_both_configs(
        monkeypatch,
        lambda: _chat_memory_stub(
            proactive_enabled=False,
            context_messages_limit=10,
            summary_max_buffer_size=500,
            memory_search_limit=3,
            bot_nickname="小鞠",
            memory_agent_lock_timeout_seconds=5,
            global_interaction_enabled=True,
            global_interaction_trigger_size=20,
            image_understanding_mode="native",
            error_notify_enabled=False,
        ),
    )

    async def _fake_build_prompt(**_kwargs: object) -> list[dict[str, object]]:
        return [{"role": "user", "content": "test"}]

    async def _fake_generate(**_kwargs: object) -> object:
        return import_module("komari_bot.plugins.komari_chat.services.chat_result").ReplyResult(
            content="送达测试",
            favorability_delta=1,
            favorability_reason="测试",
            hindsight_target_fingerprint="a" * 64,
        )


    current_message = MessageSchema(
        user_id="user-commit",
        user_nickname="commit用户",
        group_id="group-commit",
        content="commit测试",
        timestamp=1.0,
        message_id="msg-commit",
    )

    result = asyncio.run(
        handler._attempt_reply(
            bot_self_id="bot-1",
            adapter_name="OneBot V11",
            message=current_message,
            reply_to_message_id=current_message.message_id,
            image_urls=None,
            reply_context=None,
            reply_context_requested=False,
            reply_context_refetched=False,
            force_reply=True,
            reason="platform_summon",
            recall_session=_attempt_recall_session(handler),
            store_current=True,
        )
    )

    pending_reply = result[0]
    assert pending_reply is not None

    # 验证 PendingReply 无 on_reply_triggered 字段
    assert not hasattr(pending_reply, "on_reply_triggered")
    # KOMARIBOT-11：无表情回调时 reaction_sent 为真实派发结果 False
    assert pending_reply.reaction_sent is False


def test_read_buffers_failure_returns_failure_with_reaction_sent_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """read_buffers阶段异常→ failure.reaction_sent=False。"""
    redis = _FakeRedisForDebug()
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = redis
    handler.hindsight_memory = FakeHindsightMemory()

    _patch_both_configs(
        monkeypatch,
        lambda: SimpleNamespace(
            proactive_enabled=False,
            context_messages_limit=10,
            bot_nickname="小鞠",
            face_reaction_enabled=True,
            face_reaction_id="76",
            error_notify_enabled=False,
        ),
    )

    async def _failing_read_buffers(**_kwargs: object) -> tuple[list, bool]:
        msg = "Redis 读缓冲失败"
        raise RuntimeError(msg)

    monkeypatch.setattr(handler, "_read_buffers", _failing_read_buffers)

    current_message = MessageSchema(
        user_id="user-read",
        user_nickname="read用户",
        group_id="group-read",
        content="read测试",
        timestamp=1.0,
        message_id="msg-read",
    )

    _pending, _stored, failure = asyncio.run(
        handler._attempt_reply(
            bot_self_id="bot-1",
            adapter_name="OneBot V11",
            message=current_message,
            reply_to_message_id=current_message.message_id,
            image_urls=None,
            reply_context=None,
            reply_context_requested=False,
            reply_context_refetched=False,
            force_reply=True,
            reason="platform_summon",
            recall_session=_attempt_recall_session(handler),
            store_current=True,
        )
    )

    assert failure is not None
    assert failure.stage == "read_buffers"
    assert failure.reaction_sent is False


def test_pending_reply_freezes_bot_nickname_from_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TSK-192 phase B: PendingReply freezes identity from context ownership."""
    handler, message = _wire_reaction_sent_case(
        monkeypatch,
        face_reaction_enabled=False,
    )
    monkeypatch.setattr(
        message_handler_module,
        "get_context_config",
        lambda: SimpleNamespace(bot_nickname="冻结昵称"),
    )

    pending_reply, _stored, failure = _run_reaction_sent_attempt(handler, message)

    assert failure is None
    assert pending_reply is not None
    assert pending_reply.bot_nickname == "冻结昵称"
