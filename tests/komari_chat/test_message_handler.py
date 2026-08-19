"""Komari Chat 消息处理器测试。"""

from __future__ import annotations

import ast
import asyncio
import inspect
import sys
import types
from contextlib import asynccontextmanager
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Protocol, cast

from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.adapters.onebot.v11.event import Reply, Sender

import komari_bot.plugins as plugins_package
from komari_bot.plugins.agent_run_logger.diagnostic import LLMDiagnosticCollector
from komari_bot.plugins.komari_memory.services.redis_manager import MessageSchema

if TYPE_CHECKING:
    from collections.abc import Callable

import pytest

message_handler_module = import_module(
    "komari_bot.plugins.komari_chat.handlers.message_handler"
)
llm_service_module = import_module("komari_bot.plugins.komari_chat.services.llm_service")
proactive_reservation_module = import_module(
    "komari_bot.plugins.komari_chat.services.proactive_reservation"
)
ReservationDenied = proactive_reservation_module.ReservationDenied
ReservationLostError = proactive_reservation_module.ReservationLostError


def _patch_both_configs(
    monkeypatch: "pytest.MonkeyPatch",
    stub: "Callable[[], object]",
) -> None:
    """KOMARIBOT-7：迁出字段走 get_config、memory 字段走 get_memory_config。

    两个名字都 patch 为同一鸭子类型桩：被测路径只会从各自名字读取
    其字段，桩内同时持有迁出字段与 memory 字段时两处都能命中。
    """
    monkeypatch.setattr(message_handler_module, "get_config", stub)
    monkeypatch.setattr(message_handler_module, "get_memory_config", stub)


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
    """合并 memory 字段与 TSK-192 预算字段的配置替身。

    普通/debug 入口任务开始读取同一执行预算快照：stub 同时承载
    ``get_config`` 与 ``get_memory_config`` 两个名字的字段。
    """
    values: dict[str, object] = {
        "proactive_enabled": False,
        "context_messages_limit": 10,
        "context_max_utf8_bytes": 24_000,
        "context_max_estimated_tokens": 6_000,
        "summary_max_buffer_size": 500,
        "memory_search_limit": 3,
        "bot_nickname": "小鞠",
        "memory_agent_lock_timeout_seconds": 5,
        "global_interaction_enabled": True,
        "global_interaction_trigger_size": 20,
        "face_reaction_enabled": False,
        "face_reaction_id": "76",
        "image_understanding_mode": "delegated",
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


def test_resolve_trigger_message_detects_reply_to_bot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = _build_handler()
    _patch_config(monkeypatch)
    reply = _build_reply(
        sender_user_id=669293859,
        message=Message("机器人上一条回复"),
    )

    at_trigger, message_content = handler._resolve_trigger_message(
        _FakeEvent("接着说", reply=reply)
    )

    assert at_trigger is True
    assert message_content == "接着说"


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

    recent, _interactions, stored = asyncio.run(
        handler._read_buffers(
            group_id="g1",
            user_id="u2",
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


def test_attempt_reply_only_rewrites_current_message(
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
        SimpleNamespace(
            is_search_available=lambda **_kwargs: False,
            is_fetch_available=lambda **_kwargs: False,
        ),
    )
    embedding_package_name = "komari_bot.plugins.embedding_provider"
    embedding_fake = types.ModuleType(embedding_package_name)
    embedding_fake.embed = _FakeEmbeddingProvider().embed  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, embedding_package_name, embedding_fake)
    monkeypatch.setattr(
        plugins_package, "embedding_provider", embedding_fake, raising=False
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
    assert generate_with_tools_kwargs["allowed_profile_user_ids"] == frozenset(
        {"user-1", "user-2"}
    )
    assert generate_with_tools_kwargs["caller_user_id"] == "user-1"
    assert generate_with_tools_kwargs["caller_group_id"] == "group-1"
    assert generate_with_tools_kwargs["caller_is_superuser"] is False
    assert generate_with_tools_kwargs["tools"] is not None
    assert "max_tool_rounds" not in generate_with_tools_kwargs, (
        "普通入口不得传递入口专属轮次封顶（TSK-192）"
    )
    injected_favorability = cast("SimpleNamespace", build_prompt_kwargs["favorability"])
    assert injected_favorability.favorability == 0
    assert generate_with_tools_kwargs["max_favorability_delta"] == 5
    assert redis.pushed_global_interactions == []

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
    memory = _FakeMemoryForDebug()
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = redis
    handler.memory = memory
    handler._reaction_tasks = set()
    handler.query_rewrite = _FakeQueryRewrite()

    async def _fake_build_prompt(**_kwargs: object) -> list[dict[str, object]]:
        return [{"role": "user", "content": "test"}]

    async def _fake_generate(**_kwargs: object) -> object:
        return llm_service_module.ReplyResult(
            content="生成成功",
            interaction_history={"event": "测试", "result": "生成成功", "emotion": "平静"},
            favorability_delta=1,
            favorability_reason="测试",
        )

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
    monkeypatch.setattr(
        message_handler_module,
        "komari_search_plugin",
        SimpleNamespace(
            is_search_available=lambda **_kwargs: False,
            is_fetch_available=lambda **_kwargs: False,
        ),
    )
    monkeypatch.setattr(message_handler_module, "build_prompt", _fake_build_prompt)
    monkeypatch.setattr(
        message_handler_module, "generate_reply_with_tools", _fake_generate
    )
    monkeypatch.setattr(message_handler_module, "generate_reply", _fake_generate)

    embedding_package_name = "komari_bot.plugins.embedding_provider"
    embedding_fake = types.ModuleType(embedding_package_name)
    embedding_fake.embed = _FakeEmbeddingProvider().embed  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, embedding_package_name, embedding_fake)
    monkeypatch.setattr(
        plugins_package, "embedding_provider", embedding_fake, raising=False
    )
    monkeypatch.setattr(
        message_handler_module, "user_data_plugin", _FakeUserDataForDebug()
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
            reason="at",
            reply_score=1.0,
            store_current=True,
            on_reply_triggered=_dummy_reaction,
        )
    )


def test_pending_reply_reaction_sent_true_when_reaction_dispatched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ KOMARIBOT-11：表情真实派发时 PendingReply.reaction_sent=True。"""
    handler, message = _wire_reaction_sent_case(
        monkeypatch, face_reaction_enabled=True
    )
    pending_reply, stored, failure = _run_reaction_sent_attempt(handler, message)
    assert failure is None
    assert stored is True
    assert pending_reply is not None
    assert pending_reply.reaction_sent is True


def test_pending_reply_reaction_sent_false_when_reaction_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ KOMARIBOT-11：face_reaction_enabled=False 边界下 reaction_sent=False。"""
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
        SimpleNamespace(
            is_search_available=lambda **_kwargs: False,
            is_fetch_available=lambda **_kwargs: False,
        ),
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

    embedding_package_name = "komari_bot.plugins.embedding_provider"
    embedding_fake = types.ModuleType(embedding_package_name)
    embedding_fake.embed = _FakeEmbeddingProvider().embed  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, embedding_package_name, embedding_fake)
    monkeypatch.setattr(
        plugins_package, "embedding_provider", embedding_fake, raising=False
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
    # debug 路径不触达预占 module（冷却/频控零副作用）：本测试未布线
    # proactive_reservation，任何预占触达都会以 AttributeError 红灯
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
        SimpleNamespace(
            is_search_available=lambda **_kwargs: False,
            is_fetch_available=lambda **_kwargs: False,
        ),
    )
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

    async def _fake_build_prompt(**_kwargs: object) -> list[dict[str, object]]:
        return [{"role": "user", "content": "test"}]

    collector_from_generate: object = None
    trace_id_from_generate: str | None = None

    async def _fake_generate_with_tools(**kwargs: object) -> object:
        nonlocal collector_from_generate, trace_id_from_generate
        collector_from_generate = kwargs.get("collector")
        trace_id_from_generate = cast("str | None", kwargs.get("request_trace_id"))
        assert "max_tool_rounds" not in kwargs, "debug 入口不得传递入口专属轮次封顶（TSK-192）"
        return llm_service_module.ReplyResult(
            content="带trace的回复",
            interaction_history={"event": "trace", "result": "trace回复", "emotion": "平静"},
            favorability_delta=2,
            favorability_reason="trace测试",
        )

    monkeypatch.setattr(message_handler_module, "build_prompt", _fake_build_prompt)
    monkeypatch.setattr(message_handler_module, "generate_reply_with_tools", _fake_generate_with_tools)
    monkeypatch.setattr(message_handler_module, "generate_reply", _fake_generate_with_tools)

    embedding_package_name = "komari_bot.plugins.embedding_provider"
    embedding_fake = types.ModuleType(embedding_package_name)
    embedding_fake.embed = _FakeEmbeddingProvider().embed  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, embedding_package_name, embedding_fake)
    monkeypatch.setattr(
        plugins_package, "embedding_provider", embedding_fake, raising=False
    )

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
        SimpleNamespace(
            is_search_available=lambda **_kwargs: False,
            is_fetch_available=lambda **_kwargs: False,
        ),
    )
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
    download_batches: list[list[str]] = []

    async def _download_images(
        urls: list[str],
        _policy: object,
    ) -> list[str | None]:
        download_batches.append(urls)
        return [f"base64:{url}" for url in urls]

    monkeypatch.setattr(
        message_handler_module,
        "download_images_as_base64_aligned",
        _download_images,
    )

    embedding_package_name = "komari_bot.plugins.embedding_provider"
    embedding_fake = types.ModuleType(embedding_package_name)
    embedding_fake.embed = _FakeEmbeddingProvider().embed  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, embedding_package_name, embedding_fake)
    monkeypatch.setattr(
        plugins_package, "embedding_provider", embedding_fake, raising=False
    )

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
    assert download_batches == [
        ["https://example.com/ref.png", "https://example.com/img.png"]
    ]
    assert build_prompt_kwargs.get("reply_context") is reply_ctx
    assert build_prompt_kwargs.get("reply_image_urls") == [
        "base64:https://example.com/ref.png"
    ]
    assert build_prompt_kwargs.get("image_urls") == [
        "base64:https://example.com/img.png"
    ]


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
        SimpleNamespace(
            is_search_available=lambda **_kwargs: False,
            is_fetch_available=lambda **_kwargs: False,
        ),
    )
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

    embedding_package_name = "komari_bot.plugins.embedding_provider"
    embedding_fake = types.ModuleType(embedding_package_name)
    embedding_fake.embed = _FakeEmbeddingProvider().embed  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, embedding_package_name, embedding_fake)
    monkeypatch.setattr(
        plugins_package, "embedding_provider", embedding_fake, raising=False
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

    assert fake_user_data.adjust_calls == []
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

    async def _fake_read_buffers(**_kwargs: object) -> tuple[list, list, bool]:
        return [], [], True

    async def _fake_generate_core(**_kwargs: object) -> object:
        return llm_service_module.ReplyResult(
            content="主动回复",
            interaction_history=None,
            favorability_delta=0,
            favorability_reason="主动关心",
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
            reply_score=0.95,
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

    async def _fake_read_buffers(**_kwargs: object) -> tuple[list, list, bool]:
        return [], [], True

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
            reply_score=0.95,
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

    async def _fake_read_buffers(**_kwargs: object) -> tuple[list, list, bool]:
        return [], [], True

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
            reason="at",
            reply_score=1.0,
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


# ── 表情反应前置 + 失败分流 测试 ──


def test_reaction_scheduled_before_generate_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证表情反应回调在 _generate_reply_core 之前通过 create_task 派发。"""
    reaction_called = False

    redis = _FakeRedisForDebug()
    memory = _FakeMemoryForDebug()
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = redis
    handler.memory = memory
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
    monkeypatch.setattr(
        message_handler_module,
        "komari_search_plugin",
        SimpleNamespace(
            is_search_available=lambda **_kwargs: False,
            is_fetch_available=lambda **_kwargs: False,
        ),
    )

    async def _fake_build_prompt(**_kwargs: object) -> list[dict[str, object]]:
        return [{"role": "user", "content": "test"}]

    async def _fake_generate(**_kwargs: object) -> object:
        return llm_service_module.ReplyResult(
            content="带表情的回复",
            interaction_history={"event": "表情测试", "result": "表情回复", "emotion": "平静"},
            favorability_delta=1,
            favorability_reason="表情互动",
        )

    async def _fake_reaction() -> None:
        nonlocal reaction_called
        reaction_called = True

    monkeypatch.setattr(message_handler_module, "build_prompt", _fake_build_prompt)
    monkeypatch.setattr(message_handler_module, "generate_reply_with_tools", _fake_generate)
    monkeypatch.setattr(message_handler_module, "generate_reply", _fake_generate)

    embedding_package_name = "komari_bot.plugins.embedding_provider"
    embedding_fake = types.ModuleType(embedding_package_name)
    embedding_fake.embed = _FakeEmbeddingProvider().embed  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, embedding_package_name, embedding_fake)
    monkeypatch.setattr(
        plugins_package, "embedding_provider", embedding_fake, raising=False
    )

    monkeypatch.setattr(message_handler_module, "user_data_plugin", _FakeUserDataForDebug())

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
            reason="at",
            reply_score=1.0,
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
    memory = _FakeMemoryForDebug()
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = redis
    handler.memory = memory
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
    monkeypatch.setattr(
        message_handler_module,
        "komari_search_plugin",
        SimpleNamespace(
            is_search_available=lambda **_kwargs: False,
            is_fetch_available=lambda **_kwargs: False,
        ),
    )

    async def _fake_build_prompt(**_kwargs: object) -> list[dict[str, object]]:
        return [{"role": "user", "content": "test"}]

    async def _fake_generate(**_kwargs: object) -> object:
        return llm_service_module.ReplyResult(
            content="无表情回复",
            interaction_history={"event": "无表情", "result": "无表情回复", "emotion": "平静"},
            favorability_delta=1,
            favorability_reason="测试",
        )

    async def _fake_reaction() -> None:
        nonlocal reaction_called
        reaction_called = True

    monkeypatch.setattr(message_handler_module, "build_prompt", _fake_build_prompt)
    monkeypatch.setattr(message_handler_module, "generate_reply_with_tools", _fake_generate)
    monkeypatch.setattr(message_handler_module, "generate_reply", _fake_generate)

    embedding_package_name = "komari_bot.plugins.embedding_provider"
    embedding_fake = types.ModuleType(embedding_package_name)
    embedding_fake.embed = _FakeEmbeddingProvider().embed  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, embedding_package_name, embedding_fake)
    monkeypatch.setattr(
        plugins_package, "embedding_provider", embedding_fake, raising=False
    )
    monkeypatch.setattr(message_handler_module, "user_data_plugin", _FakeUserDataForDebug())

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
            reason="at",
            reply_score=1.0,
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
    memory = _FakeMemoryForDebug()
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = redis
    handler.memory = memory
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
    monkeypatch.setattr(
        message_handler_module,
        "komari_search_plugin",
        SimpleNamespace(
            is_search_available=lambda **_kwargs: False,
            is_fetch_available=lambda **_kwargs: False,
        ),
    )

    async def _fake_build_prompt(**_kwargs: object) -> list[dict[str, object]]:
        return [{"role": "user", "content": "test"}]

    async def _fake_generate(**_kwargs: object) -> object:
        return llm_service_module.ReplyResult(
            content="",  # 空回复
            interaction_history={"event": "空回复测试", "result": "", "emotion": "平静"},
            favorability_delta=1,
            favorability_reason="测试",
        )

    monkeypatch.setattr(message_handler_module, "build_prompt", _fake_build_prompt)
    monkeypatch.setattr(message_handler_module, "generate_reply_with_tools", _fake_generate)
    monkeypatch.setattr(message_handler_module, "generate_reply", _fake_generate)

    embedding_package_name = "komari_bot.plugins.embedding_provider"
    embedding_fake = types.ModuleType(embedding_package_name)
    embedding_fake.embed = _FakeEmbeddingProvider().embed  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, embedding_package_name, embedding_fake)
    monkeypatch.setattr(
        plugins_package, "embedding_provider", embedding_fake, raising=False
    )
    monkeypatch.setattr(message_handler_module, "user_data_plugin", _FakeUserDataForDebug())

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
            reason="at",
            reply_score=1.0,
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
    memory = _FakeMemoryForDebug()
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = redis
    handler.memory = memory
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
    monkeypatch.setattr(
        message_handler_module,
        "komari_search_plugin",
        SimpleNamespace(
            is_search_available=lambda **_kwargs: False,
            is_fetch_available=lambda **_kwargs: False,
        ),
    )

    async def _fake_build_prompt(**_kwargs: object) -> list[dict[str, object]]:
        return [{"role": "user", "content": "test"}]

    async def _fake_generate(**_kwargs: object) -> object:
        return llm_service_module.ReplyResult(
            content="有回复但无delta",
            interaction_history={"event": "delta测试", "result": "有回复无delta", "emotion": "平静"},
            favorability_delta=None,  # 缺失
            favorability_reason=None,
        )

    monkeypatch.setattr(message_handler_module, "build_prompt", _fake_build_prompt)
    monkeypatch.setattr(message_handler_module, "generate_reply_with_tools", _fake_generate)
    monkeypatch.setattr(message_handler_module, "generate_reply", _fake_generate)

    embedding_package_name = "komari_bot.plugins.embedding_provider"
    embedding_fake = types.ModuleType(embedding_package_name)
    embedding_fake.embed = _FakeEmbeddingProvider().embed  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, embedding_package_name, embedding_fake)
    monkeypatch.setattr(
        plugins_package, "embedding_provider", embedding_fake, raising=False
    )
    monkeypatch.setattr(message_handler_module, "user_data_plugin", _FakeUserDataForDebug())

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
            reason="at",
            reply_score=1.0,
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
    handler.memory = _FakeMemoryForDebug()
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
            reply_score=0.95,
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
            reply_score=0.95,
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
    memory = _FakeMemoryForDebug()
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
    handler.memory = memory
    handler._reaction_tasks = set()
    handler.proactive_reservation = reservation_svc

    async def _fake_read_buffers(**_kwargs: object) -> tuple[list, list, bool]:
        return [], [], True

    async def _fake_generate_core(**_kwargs: object) -> object:
        return llm_service_module.ReplyResult(
            content="生成成功但租约已丢",
            interaction_history={"event": "测试", "result": "生成成功", "emotion": "平静"},
            favorability_delta=1,
            favorability_reason="测试",
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
            reply_score=0.95,
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
        SimpleNamespace(
            is_search_available=lambda **_kwargs: False,
            is_fetch_available=lambda **_kwargs: False,
        ),
    )

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
        return llm_service_module.ReplyResult(
            content="送达测试",
            interaction_history={"event": "送达", "result": "送达回复", "emotion": "平静"},
            favorability_delta=1,
            favorability_reason="测试",
        )

    monkeypatch.setattr(message_handler_module, "build_prompt", _fake_build_prompt)
    monkeypatch.setattr(message_handler_module, "generate_reply_with_tools", _fake_generate)
    monkeypatch.setattr(message_handler_module, "generate_reply", _fake_generate)

    embedding_package_name = "komari_bot.plugins.embedding_provider"
    embedding_fake = types.ModuleType(embedding_package_name)
    embedding_fake.embed = _FakeEmbeddingProvider().embed  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, embedding_package_name, embedding_fake)
    monkeypatch.setattr(
        plugins_package, "embedding_provider", embedding_fake, raising=False
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
            reason="at",
            reply_score=1.0,
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
    handler.memory = _FakeMemoryForDebug()

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

    async def _failing_read_buffers(**_kwargs: object) -> tuple[list, list, bool]:
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
            reason="at",
            reply_score=1.0,
            store_current=True,
        )
    )

    assert failure is not None
    assert failure.stage == "read_buffers"
    assert failure.reaction_sent is False
