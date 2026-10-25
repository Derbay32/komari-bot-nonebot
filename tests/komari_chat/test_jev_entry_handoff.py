"""TSK-187 真实入口交接验收：实际 NoneBot 分发 + 真实判定引擎。

每次分发都经真实 ``nonebot.message.handle_event``，命中生产
``group_history_summary``（priority 9）与 ``komari_chat``（priority 10）
matcher；判定侧使用生产 ``JevDecisionEngine``、真实 PostgreSQL/Redis 与官方
SDK ``httpx2`` transport 替身。只替换外部 I/O（模型传输、回复生成、平台发送、
总结执行与配置读取），不替换判定业务、抽签、持久去重或交接实现。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from contextlib import asynccontextmanager, contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Self, cast
from uuid import uuid4

import httpx2
import pytest
from loguru import logger as loguru_logger
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.adapters.onebot.v11.event import GroupMessageEvent, Reply, Sender
from nonebot.matcher import matchers
from nonebot.message import handle_event
from nonebot.rule import TrieRule

import komari_bot.plugins.group_history_summary as summary_module
import komari_bot.plugins.komari_decision as decision_module
from komari_bot.onebot import group_failure_notify as shared_notify_module
from komari_bot.plugins.komari_chat.handlers import (
    message_handler as handler_module,
)
from komari_bot.plugins.komari_chat.handlers.message_handler import (
    MessageHandler,
    ReplyResult,
)
from komari_bot.plugins.komari_decision.services import jev_dispatch
from tests.komari_chat.hindsight_recall_support import FakeHindsightMemory
from tests.komari_decision.jev_test_support import (
    DecisionHarness,
    HarnessOptions,
    build_answers,
    create_scratch_database,
    drop_scratch_database,
    note_scope,
    read_dispatch,
    skip_without_services,
    update_config_row,
    wait_until,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator

    from komari_bot.decision.dispatch import DecisionDispatch
    from komari_bot.decision.jev import (
        DecisionRequest,
        DecisionResult,
        HandoffRoute,
    )

pytestmark = pytest.mark.asyncio

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BOT_SELF_ID = 12345
_USER_ID = 67890
_SUMMARY_TEXT = "帮我总结一下今天群里聊了什么"
_NORMAL_TEXT = "今天谁值班？"
_TRANSPORT_DOWN = "transport down"


@pytest.fixture(scope="module")
def decision_database() -> Iterator[str]:
    skip_without_services()
    url = asyncio.run(create_scratch_database(f"_tsk187{uuid4().hex[:8]}"))
    yield url
    asyncio.run(drop_scratch_database(url))


def _summary_options(
    *,
    summary: float = 0.9,
    reply: str = "required",
) -> HarnessOptions:
    """总结命中/未命中的受控答案（手写字面量，不导入生产阈值）。"""
    return HarnessOptions(
        responder=lambda request, _index: build_answers(
            _questions_of(request), reply=reply, summary=summary
        ),
    )


def _questions_of(request: httpx2.Request) -> dict[str, Any]:
    body = json.loads(request.content)
    return cast("dict[str, Any]", body["questions"])


def _chat_entry_module() -> Any:
    """按源文件加载真实聊天插件入口（测试不执行包级 require 副作用）。"""
    module_name = "komari_bot.plugins.komari_chat._tsk187_entry"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(
        module_name,
        _PROJECT_ROOT / "komari_bot/plugins/komari_chat/__init__.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _isolate_entry_matchers() -> Iterator[None]:
    """只保留生产 summary/chat matcher，退出时精确恢复 registry 与 lifespan。"""
    chat_entry = _chat_entry_module()
    from nonebot import get_driver

    lifespan = get_driver()._lifespan
    matcher_snapshot = {priority: list(group) for priority, group in matchers.items()}
    startup_snapshot = list(lifespan._startup_funcs)
    shutdown_snapshot = list(lifespan._shutdown_funcs)
    keep = {summary_module.summary_matcher, chat_entry.matcher}
    for priority in list(matchers):
        matchers[priority] = [m for m in matchers[priority] if m in keep]
    try:
        yield
    finally:
        matchers.clear()
        matchers.update(matcher_snapshot)
        lifespan._startup_funcs[:] = startup_snapshot
        lifespan._shutdown_funcs[:] = shutdown_snapshot


class _ProbeBot(OneBotV11Bot):
    """真实 OneBot V11 Bot 身份；平台发送只记录，不触网。"""

    def __init__(self, self_id: str = str(_BOT_SELF_ID)) -> None:
        adapter = cast("OneBotV11Adapter", OneBotV11Adapter.__new__(OneBotV11Adapter))
        super().__init__(adapter, self_id)
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.sent: list[object] = []
        self.get_msg_payload: dict[str, object] = {"message_id": 424242}

    async def call_api(self, api: str, **data: object) -> object:
        self.calls.append((api, data))
        if api == "get_msg":
            return dict(self.get_msg_payload)
        return {"message_id": 424242}

    async def send(self, event: object, message: object, **_kwargs: object) -> object:
        del event
        self.sent.append(message)
        return {"message_id": 424242}


def _message(*segments: MessageSegment | str) -> Message:
    message = Message()
    for segment in segments:
        message += segment
    return message


def _event(
    *,
    group_id: int,
    message_id: int,
    original: Message,
    reply: Reply | None = None,
    to_me: bool = False,
) -> GroupMessageEvent:
    return cast(
        "GroupMessageEvent",
        GroupMessageEvent.model_construct(
            time=int(datetime.now(UTC).timestamp()),
            self_id=_BOT_SELF_ID,
            post_type="message",
            sub_type="normal",
            user_id=_USER_ID,
            message_type="group",
            message_id=message_id,
            message=original,
            original_message=original,
            raw_message="raw",
            font=0,
            sender=Sender.model_construct(
                user_id=_USER_ID, nickname="群友", card="群友"
            ),
            to_me=to_me,
            reply=reply,
            group_id=group_id,
            anonymous=None,
        ),
    )


class _Lease:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def handoff(self) -> object:
        return object()


class _Reservation:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def reserve(self, group_id: str, reservation_id: str) -> _Lease:
        self.calls.append((group_id, reservation_id))
        return _Lease()


class _CooldownRedis:
    """只实现共享通知冷却需要的 ``SET NX EX`` 语义（严格形状，不静默成功）。"""

    def __init__(self) -> None:
        self.keys: dict[str, str] = {}
        self.set_calls: list[tuple[str, str, bool, int]] = []

    async def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        ex: int | None = None,
    ) -> bool | None:
        if not isinstance(key, str) or not key:
            msg = "共享通知冷却键必须是非空字符串"
            raise TypeError(msg)
        if nx is not True or ex is None:
            msg = "共享通知冷却必须使用 SET NX EX 形状"
            raise TypeError(msg)
        self.set_calls.append((key, value, nx, ex))
        if key in self.keys:
            return None
        self.keys[key] = value
        return True


class _ChatRedis:
    def __init__(self) -> None:
        self.pushed: list[Any] = []
        # 共享通知冷却的 Redis 客户端（既有 RedisFailureNotificationCooldown 形状）。
        self.redis = _CooldownRedis()

    async def push_message(self, _group_id: str, message: Any) -> None:
        self.pushed.append(message)

    async def get_buffer(self, _group_id: str, *, limit: int = 10) -> list[Any]:
        del limit
        return []

    async def get_global_interaction_buffer(
        self, _user_id: str, *, limit: int = 10
    ) -> list[dict[str, object]]:
        del limit
        return []


class _Fulfillment:
    """聊天回复履约窄边界替身：只记录，不发送。

    ``on_duplicate_check`` 用于在「判定已完成、交接尚未领取」的既有入口时序
    点上注入外部事实（例如群撤权），不越过也不复制判定或交接语义。
    """

    def __init__(self) -> None:
        self.pending: list[Any] = []
        self.on_duplicate_check: Callable[[], None] | None = None

    async def is_duplicate_event(self, _fulfillment_id: str) -> bool:
        if self.on_duplicate_check is not None:
            self.on_duplicate_check()
        return False

    async def fulfill(self, pending_reply: Any, **_kwargs: object) -> bool:
        self.pending.append(pending_reply)
        return True


class _BanPlugin:
    class BanServiceUnavailableError(RuntimeError):
        pass

    def __init__(self, banned: bool) -> None:  # noqa: FBT001 - 测试替身固定开关
        self.banned = banned

    async def is_event_banned(self, _bot: object, _event: object, _scope: str) -> bool:
        return self.banned


def _admit_all(*_args: object, **_kwargs: object) -> SimpleNamespace:
    from komari_bot.plugins.group_admission import AdmissionQualification

    return SimpleNamespace(qualification=AdmissionQualification.BUSINESS)


def _admit_asset(**_kwargs: object) -> bool:
    return True


class _World:
    def __init__(self, *, group_id: int) -> None:
        self.group_id = group_id
        self.bot = _ProbeBot()
        self.entry = _chat_entry_module()
        self.redis = _ChatRedis()
        self.fulfillment = _Fulfillment()
        self.reservation = _Reservation()
        self.summary_executions: list[dict[str, object]] = []
        self.generation_kwargs: list[dict[str, Any]] = []
        self.memory = FakeHindsightMemory()
        self.next_message_id = 900_000
        self.memory_config: SimpleNamespace | None = None
        self.superusers: set[str] = set()

    @property
    def get_msg_calls(self) -> list[dict[str, object]]:
        """生成上下文补取的平台读取记录（`get_msg`）。"""
        return [data for api, data in self.bot.calls if api == "get_msg"]

    def event(
        self,
        *,
        text: str | None = None,
        segments: tuple[MessageSegment | str, ...] | None = None,
        reply: Reply | None = None,
        to_me: bool = False,
        message_id: int | None = None,
    ) -> GroupMessageEvent:
        if message_id is None:
            self.next_message_id += 1
            message_id = self.next_message_id
        original = (
            _message(*segments)
            if segments is not None
            else _message(text if text is not None else _NORMAL_TEXT)
        )
        return _event(
            group_id=self.group_id,
            message_id=message_id,
            original=original,
            reply=reply,
            to_me=to_me,
        )


def _install_summary(monkeypatch: pytest.MonkeyPatch, world: _World) -> None:
    summary_config = SimpleNamespace(
        plugin_enable=True,
        min_summary_count=10,
        max_summary_count=200,
    )
    assert world.memory_config is not None
    memory_config = world.memory_config
    monkeypatch.setattr(
        summary_module,
        "config_manager",
        SimpleNamespace(get=lambda: summary_config),
    )
    # TSK-188：总结失败通知经既有 config_manager factory 读取 komari_context 资源。
    monkeypatch.setattr(
        summary_module,
        "_context_config_manager",
        SimpleNamespace(get=lambda: memory_config),
        raising=False,
    )
    monkeypatch.setattr(summary_module, "adjudicate", _admit_all)
    monkeypatch.setattr(summary_module, "effect_business_admitted", _admit_asset)
    monkeypatch.setattr(summary_module, "get_decision_engine", _live_engine)
    monkeypatch.setattr(
        summary_module, "get_decision_dispatch", jev_dispatch.get_decision_dispatch
    )

    async def _supported(_bot: object) -> bool:
        return True

    async def _execute(**kwargs: object) -> object:
        world.summary_executions.append(dict(kwargs))
        return SimpleNamespace(
            image_base64="",
            image_pages_base64=(),
            summary_text="总结正文",
            filtered_message_count=3,
            plan_result=None,
            image_truncated=False,
            filter_label="最近消息",
            time_range="now",
            history_fetch=None,
        )

    monkeypatch.setattr(summary_module, "check_group_history_supported", _supported)
    monkeypatch.setattr(summary_module, "execute_group_summary", _execute)


def _live_engine() -> object | None:
    return decision_module.get_decision_runtime().engine


@asynccontextmanager
async def _world(
    monkeypatch: pytest.MonkeyPatch,
    database: str,
    *,
    options: HarnessOptions | None = None,
    ban: bool = False,
    notify: bool = False,
    superusers: set[str] | None = None,
) -> AsyncIterator[tuple[_World, DecisionHarness]]:
    """装配真实引擎 + 真实入口 + 外部 I/O 替身的一体化世界。"""
    group_id = 700_000 + uuid4().int % 100_000
    note_scope("onebot_v11", str(_BOT_SELF_ID), str(group_id))
    world = _World(group_id=group_id)
    async with DecisionHarness(database, options or HarnessOptions()) as harness:
        assert harness.decision is not None
        runtime = decision_module.get_decision_runtime()
        monkeypatch.setattr(runtime, "_engine", harness.decision)
        monkeypatch.setattr(runtime, "_closed", False)

        entry = world.entry
        handler = MessageHandler(
            redis=cast("Any", world.redis),
            hindsight_memory=cast("Any", world.memory),
            reply_fulfillment=cast("Any", world.fulfillment),
            proactive_reservation=cast("Any", world.reservation),
            decision_engine=harness.decision,
        )

        async def _generate(**kwargs: object) -> ReplyResult:
            world.generation_kwargs.append(dict(kwargs))
            return ReplyResult(
                content="回复正文",
                favorability_delta=0,
                favorability_reason="测试",
                hindsight_target_fingerprint="a" * 64,
            )

        memory_config = SimpleNamespace(
            plugin_enable=True,
            face_reaction_enabled=False,
            face_reaction_id=None,
            error_notify_enabled=notify,
            bot_nickname="小鞠",
            bot_aliases=["小鞠", "小鞠知花", "komari"],
            context_messages_limit=10,
            context_max_utf8_bytes=24_000,
            context_max_estimated_tokens=6_000,
        )
        world.memory_config = memory_config
        # 共享失败通知器的运行时 SUPERUSER 解析：只替换配置输入，不复制解析逻辑。
        recipients = set(superusers) if superusers is not None else {"10001"}
        world.superusers = recipients
        monkeypatch.setattr(
            shared_notify_module,
            "get_driver",
            lambda: SimpleNamespace(config=SimpleNamespace(superusers=recipients)),
        )
        memory_config.plugin_enable = True
        memory_config.proactive_enabled = True
        chat_config = memory_config

        async def _not_superuser(_bot: object, _event: object) -> bool:
            return False

        monkeypatch.setattr(handler, "_generate_reply_core", _generate)
        monkeypatch.setattr(handler_module, "SUPERUSER", _not_superuser)
        monkeypatch.setattr(entry, "_get_or_build_handler", lambda: handler)
        monkeypatch.setattr(
            entry, "_get_or_build_reply_fulfillment", lambda: world.fulfillment
        )
        monkeypatch.setattr(entry, "user_ban_plugin", _BanPlugin(ban))
        monkeypatch.setattr(entry, "adjudicate", _admit_all)
        monkeypatch.setattr(entry, "get_context_config", lambda: memory_config)
        monkeypatch.setattr(entry, "get_config", lambda: chat_config)
        monkeypatch.setattr(handler_module, "get_context_config", lambda: memory_config)
        monkeypatch.setattr(handler_module, "get_config", lambda: chat_config)
        _install_summary(monkeypatch, world)
        try:
            yield world, harness
        finally:
            note_scope("onebot_v11", str(_BOT_SELF_ID), str(group_id))


async def _dispatch(world: _World, event: GroupMessageEvent) -> None:
    await handle_event(world.bot, event)


def _questions(harness: DecisionHarness, index: int = 0) -> list[str]:
    return harness.transport.question_ids(index)


def _target_text(harness: DecisionHarness, index: int = 0) -> str:
    conversation = harness.transport.state(index)["untrusted_group_conversation"]
    return cast("str", conversation["messages"][conversation["target"]]["text"])


async def _prepare_state(
    world: _World,
    event: GroupMessageEvent,
    state: dict[Any, Any],
) -> None:
    await jev_dispatch.prepare_decision_dispatch(
        bot=cast("Any", world.bot), event=cast("Any", event), state=cast("Any", state)
    )


async def _carrier(world: _World, event: GroupMessageEvent) -> DecisionDispatch:
    state: dict[Any, Any] = {}
    await _prepare_state(world, event, state)
    return jev_dispatch.get_decision_dispatch(
        cast("Any", world.bot), cast("Any", event), cast("Any", state)
    )


async def _dispatch_row(
    harness: DecisionHarness, world: _World, event: GroupMessageEvent
) -> dict[str, Any] | None:
    from komari_bot.decision.jev import EventIdentity

    identity = EventIdentity(
        protocol="onebot_v11",
        account_id=str(_BOT_SELF_ID),
        group_id=str(world.group_id),
        message_id=str(event.message_id),
    )
    assert harness.engine is not None
    return await read_dispatch(harness.engine, identity)


# ---------------------------------------------------------------------------
# 1-4 一次所需请求、总结与平台召唤的入口事实
# ---------------------------------------------------------------------------


async def test_normal_message_requests_two_choices_once(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _world(monkeypatch, decision_database) as (world, harness):
        event = world.event(text=_NORMAL_TEXT)

        await _dispatch(world, event)

        assert _questions(harness) == ["reply_eligibility", "retention_purpose"]
        assert _target_text(harness) == _NORMAL_TEXT
        assert len(harness.transport.requests) == 1
        assert len(world.fulfillment.pending) == 1
        assert world.fulfillment.pending[0].reason == "required"
        assert len(world.memory.retain_calls) == 1
        row = await _dispatch_row(harness, world, event)
        assert row is not None
        assert row["business_route"] == "chat"
        assert row["handed_off_at"] is not None


async def test_text_addressed_summary_candidate_requests_noul_and_two_choices_once(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _world(monkeypatch, decision_database, options=_summary_options()) as (
        world,
        harness,
    ):
        event = world.event(text=_SUMMARY_TEXT, to_me=True)

        await _dispatch(world, event)

        assert _questions(harness) == [
            "reply_eligibility",
            "retention_purpose",
            "summary_request",
        ]
        assert len(harness.transport.requests) == 1
        assert len(world.summary_executions) == 1
        assert world.summary_executions[0]["requested_count"] is None
        assert world.fulfillment.pending == []


async def test_native_platform_summary_candidate_requests_only_noul_once(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _world(monkeypatch, decision_database, options=_summary_options()) as (
        world,
        harness,
    ):
        event = world.event(
            segments=(MessageSegment.at(_BOT_SELF_ID), _SUMMARY_TEXT),
            to_me=True,
        )

        await _dispatch(world, event)

        assert _questions(harness) == ["summary_request"]
        assert len(world.summary_executions) == 1
        assert world.fulfillment.pending == []


async def test_native_non_summary_summons_makes_no_sdk_request(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _world(monkeypatch, decision_database) as (world, harness):
        event = world.event(
            segments=(MessageSegment.at(_BOT_SELF_ID), _NORMAL_TEXT),
            to_me=True,
        )

        await _dispatch(world, event)

        assert harness.transport.requests == []
        assert world.summary_executions == []
        assert len(world.fulfillment.pending) == 1
        assert world.fulfillment.pending[0].reason == "platform_summon"
        row = await _dispatch_row(harness, world, event)
        assert row is not None
        assert row["state"] == "bypass"


# ---------------------------------------------------------------------------
# 5-8 总结独占、结果复用、阈值与数量
# ---------------------------------------------------------------------------


async def test_summary_hit_executes_only_summary_after_one_claim(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _world(monkeypatch, decision_database, options=_summary_options()) as (
        world,
        harness,
    ):
        handler = world.entry._get_or_build_handler()

        async def _never(**_kwargs: object) -> None:
            msg = "总结命中后不得进入普通聊天生成"
            raise AssertionError(msg)

        monkeypatch.setattr(handler, "_attempt_reply", _never)
        event = world.event(text=_SUMMARY_TEXT, to_me=True)

        await _dispatch(world, event)

        assert len(world.summary_executions) == 1
        assert world.fulfillment.pending == []
        assert world.redis.pushed == []
        row = await _dispatch_row(harness, world, event)
        assert row is not None
        assert row["business_route"] == "summary"
        assert row["handed_off_at"] is not None


async def test_summary_miss_reuses_same_result_and_draw_in_chat(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = HarnessOptions(
        responder=lambda request, _index: build_answers(
            _questions_of(request), reply="optional", summary=0.05
        ),
        random_values=[0.1],
    )
    async with _world(monkeypatch, decision_database, options=options) as (
        world,
        harness,
    ):
        event = world.event(text=_SUMMARY_TEXT, to_me=True)

        await _dispatch(world, event)

        assert len(harness.transport.requests) == 1
        assert world.summary_executions == []
        assert len(world.fulfillment.pending) == 1
        pending = world.fulfillment.pending[0]
        assert pending.reason == "optional"
        assert pending.force_reply is False
        # 引擎只抽签一次：聊天消费的是同一份抽签结果。
        assert harness.random_draws == 1
        assert len(world.reservation.calls) == 1


@pytest.mark.parametrize(
    ("summary_probability", "summary_runs"),
    [(0.7, False), (0.71, True)],
)
async def test_summary_threshold_boundary(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
    summary_probability: float,
    summary_runs: bool,  # noqa: FBT001 - parametrize 布尔断言
) -> None:
    options = HarnessOptions(
        responder=lambda request, _index: build_answers(
            _questions_of(request), reply="required", summary=summary_probability
        ),
    )
    async with _world(monkeypatch, decision_database, options=options) as (
        world,
        harness,
    ):
        await _dispatch(world, world.event(text=_SUMMARY_TEXT, to_me=True))

        assert len(harness.transport.requests) == 1
        assert (len(world.summary_executions) == 1) is summary_runs
        assert (world.fulfillment.pending == []) is summary_runs


@pytest.mark.parametrize(
    ("text", "count"),
    [
        ("总结过去 50 条消息", 50),
        ("总结过去 500 条消息", 500),
    ],
)
async def test_numeric_summary_never_bypasses_model_and_range_checked_after_claim(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    count: int,
) -> None:
    async with _world(monkeypatch, decision_database, options=_summary_options()) as (
        world,
        harness,
    ):
        event = world.event(text=text, to_me=True)

        await _dispatch(world, event)

        assert _questions(harness) == [
            "reply_eligibility",
            "retention_purpose",
            "summary_request",
        ]
        row = await _dispatch_row(harness, world, event)
        assert row is not None
        assert row["handed_off_at"] is not None, "数量总结必须先取得总结交接资格"
        if count <= 200:
            assert world.summary_executions[0]["requested_count"] == count
        else:
            assert world.summary_executions == []
            assert any(
                str(message) == summary_module.OUT_OF_RANGE_MESSAGE
                for message in world.bot.sent
            )


# ---------------------------------------------------------------------------
# 9-12 重投、别名/召唤区分与功能命令
# ---------------------------------------------------------------------------


async def test_external_redelivery_cannot_acquire_second_handoff(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _world(monkeypatch, decision_database) as (world, harness):
        event = world.event(text=_NORMAL_TEXT)

        await _dispatch(world, event)
        await _dispatch(world, event)  # 同一个 Event 对象再次分发

        assert len(harness.transport.requests) == 1
        assert len(world.fulfillment.pending) == 1
        row = await _dispatch_row(harness, world, event)
        assert row is not None
        assert row["business_route"] == "chat"


async def test_plain_text_alias_and_to_me_do_not_bypass_choices(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_text = "@小鞠知花 我不吃药！"
    async with _world(monkeypatch, decision_database) as (world, harness):
        await _dispatch(world, world.event(text=raw_text, to_me=True))

        assert _questions(harness) == ["reply_eligibility", "retention_purpose"]
        # 判定使用未经框架剥离称呼的原始正文。
        assert _target_text(harness) == raw_text


async def test_real_reply_to_bot_bypasses_choices(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reply_id = 555_001
    reply = Reply(
        time=int(datetime.now(UTC).timestamp()) - 5,
        message_type="group",
        message_id=reply_id,
        real_id=reply_id,
        sender=Sender.model_construct(user_id=_BOT_SELF_ID, nickname="小鞠"),
        message=_message("机器人上一条回复"),
    )
    async with _world(monkeypatch, decision_database) as (world, harness):
        event = world.event(
            segments=(MessageSegment.reply(reply_id), " 接着说"),
            reply=reply,
        )

        await _dispatch(world, event)

        assert harness.transport.requests == []
        assert len(world.fulfillment.pending) == 1
        assert world.fulfillment.pending[0].reason == "platform_summon"


@pytest.mark.parametrize(
    ("native_reply_id", "reply_message_id", "reply_user_id"),
    [
        (555_101, 555_102, _BOT_SELF_ID),  # 标识不符
        (555_101, 555_101, _USER_ID),  # 引用他人（可信引用但非召唤）
    ],
)
async def test_fabricated_or_unsolicited_reply_evidence_does_not_bypass_choices(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
    native_reply_id: int,
    reply_message_id: int,
    reply_user_id: int,
) -> None:
    reply = Reply(
        time=int(datetime.now(UTC).timestamp()) - 5,
        message_type="group",
        message_id=reply_message_id,
        real_id=reply_message_id,
        sender=Sender.model_construct(user_id=reply_user_id, nickname="某人"),
        message=_message("被引用的原文"),
    )
    async with _world(monkeypatch, decision_database) as (world, harness):
        await _dispatch(
            world,
            world.event(
                segments=(MessageSegment.reply(native_reply_id), " 接着说"),
                reply=reply,
            ),
        )

        assert _questions(harness) == ["reply_eligibility", "retention_purpose"]


async def test_registered_command_is_excluded_and_command_discussion_is_ordinary(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot import on_command

    name = f"tsk187probe{uuid4().hex[:6]}"
    default_starts = ("。", ".")
    on_command(name, priority=1, block=False)
    try:
        async with _world(monkeypatch, decision_database) as (world, harness):
            await _dispatch(world, world.event(text=f".{name} 参数"))

            # 注册命令：不请求模型、不生成回复、也不当作普通聊天。
            assert harness.transport.requests == []
            assert world.fulfillment.pending == []
            assert world.redis.pushed == []

            await _dispatch(
                world,
                world.event(text=f"怎么用 .{name} 这个功能", to_me=True),
            )
            assert _questions(harness, 0) == [
                "reply_eligibility",
                "retention_purpose",
            ]
    finally:
        for start in default_starts:
            TrieRule.prefix.pop(f"{start}{name}", None)


# ---------------------------------------------------------------------------
# 13-15 封禁、保留、失败与交接边界
# ---------------------------------------------------------------------------


async def test_ban_classifies_and_stores_but_never_hands_off(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _world(monkeypatch, decision_database, ban=True) as (world, harness):
        event = world.event(text=_NORMAL_TEXT)

        await _dispatch(world, event)

        assert _questions(harness) == ["reply_eligibility", "retention_purpose"]
        assert len(world.redis.pushed) == 1
        assert len(world.memory.retain_calls) == 1
        assert world.fulfillment.pending == []
        assert world.reservation.calls == []
        row = await _dispatch_row(harness, world, event)
        assert row is not None
        assert row["handed_off_at"] is None


async def test_optional_silent_retains_message_without_reply(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = HarnessOptions(
        responder=lambda request, _index: build_answers(
            _questions_of(request), reply="optional", summary=0.05
        ),
        random_values=[0.99],
    )
    async with _world(monkeypatch, decision_database, options=options) as (
        world,
        harness,
    ):
        event = world.event(text=_NORMAL_TEXT)

        await _dispatch(world, event)

        assert harness.random_draws == 1
        assert len(world.redis.pushed) == 1
        assert len(world.memory.retain_calls) == 1
        assert world.fulfillment.pending == []
        assert world.reservation.calls == []
        row = await _dispatch_row(harness, world, event)
        assert row is not None
        assert row["handed_off_at"] is None


async def test_ordinary_technical_failure_preserves_original_message(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail(_request: httpx2.Request, _index: int) -> Any:
        raise httpx2.ConnectError(_TRANSPORT_DOWN)

    options = HarnessOptions(responder=_fail)
    async with _world(monkeypatch, decision_database, options=options) as (
        world,
        harness,
    ):
        await _dispatch(world, world.event(text=_NORMAL_TEXT))

        assert len(harness.transport.requests) == 1
        assert len(world.redis.pushed) == 1
        assert world.memory.retain_calls == []
        assert world.fulfillment.pending == []
        assert world.bot.sent == []


async def test_noul_failure_stops_both_generation_paths(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail(_request: httpx2.Request, _index: int) -> Any:
        raise httpx2.ConnectError(_TRANSPORT_DOWN)

    assert not hasattr(decision_module, "classify_summary_request"), (
        "旧总结归类 operation 必须物理退役，不能靠 monkeypatch 伪装缺席"
    )
    options = HarnessOptions(responder=_fail)
    async with _world(monkeypatch, decision_database, options=options) as (
        world,
        harness,
    ):
        await _dispatch(world, world.event(text=_SUMMARY_TEXT, to_me=True))

        assert len(harness.transport.requests) == 1
        assert world.summary_executions == []
        assert world.fulfillment.pending == []
        assert world.redis.pushed == []


async def test_group_revocation_and_inflight_disable_cancellation_prevent_late_effects(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """在途取消与撤权：与「已完成结果的持久停用栅栏」是两条不同事实。

    这里使用本地冻结配置加 ``refresh``，证明停用会取消**尚在评估**的分发；
    已完成结果在持久停用后拒绝交接、且重启启用后仍拒绝，由
    ``test_completed_result_is_fenced_by_persistent_disable_and_stays_fenced``
    用真实 ``komari_decision_config`` 提交覆盖。
    """
    admitted = {"value": True}
    gate = asyncio.Event()
    gate.set()

    def _responder(request: httpx2.Request, index: int) -> Any:
        del index

        async def _wait() -> dict[str, Any]:
            await gate.wait()
            return build_answers(_questions_of(request), reply="required")

        return _wait()

    options = HarnessOptions(
        admission=lambda _identity: admitted["value"],
        responder=_responder,
    )
    async with _world(monkeypatch, decision_database, options=options) as (
        world,
        harness,
    ):
        assert harness.decision is not None
        first = await _carrier(world, world.event(text=_NORMAL_TEXT))
        result = await first.evaluate(harness.decision, summary_candidate=False)
        assert result.route == "chat"

        # 群撤权发生在交接之前：持久裁决阻止迟到效果。
        admitted["value"] = False
        assert await first.claim_handoff("chat") is False

        # 判定停用在在途评估期间取消该分发：不产生可交接结果。
        admitted["value"] = True
        baseline = len(harness.transport.requests)
        gate.clear()
        blocked = await _carrier(world, world.event(text=_NORMAL_TEXT))
        evaluation = asyncio.create_task(
            blocked.evaluate(harness.decision, summary_candidate=False)
        )
        await wait_until(
            lambda: len(harness.transport.requests) == baseline + 1,
            within_seconds=5.0,
        )
        harness.set_config({"plugin_enable": False})
        with suppress(Exception):
            await harness.decision.refresh()
        with pytest.raises(asyncio.CancelledError):
            await evaluation
        assert await blocked.claim_handoff("chat") is False
        assert world.fulfillment.pending == []
        gate.set()


async def test_dispatch_carrier_is_unavailable_without_framework_state(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实入口不接受制造业状态：缺失持有对象时故障关闭。"""
    async with _world(monkeypatch, decision_database) as (world, harness):
        event = world.event(text=_NORMAL_TEXT)
        with pytest.raises(jev_dispatch.DecisionDispatchUnavailableError):
            jev_dispatch.get_decision_dispatch(
                cast("Any", world.bot), cast("Any", event), {}
            )
        assert harness.transport.requests == []
        assert world.fulfillment.pending == []


# ---------------------------------------------------------------------------
# 21-22 生成上下文时序与持久停用栅栏（TSK-187 rework）
# ---------------------------------------------------------------------------


def _incomplete_reply(*, reply_id: int) -> Reply:
    """一条**已可获得但正文不完整**的引用（只有图片且无可读来源）。"""
    return Reply(
        time=int(datetime.now(UTC).timestamp()) - 5,
        message_type="group",
        message_id=reply_id,
        real_id=reply_id,
        sender=Sender.model_construct(user_id=_USER_ID, nickname="群友"),
        message=_message(MessageSegment.image(file="missing.image")),
    )


def _refetch_payload(
    *,
    reply_id: int,
    group_id: int,
) -> dict[str, object]:
    """``get_msg`` 成功补取的引用原文（平台读取边界替身）。"""
    return {
        "time": int(datetime.now(UTC).timestamp()) - 4,
        "message_type": "group",
        "message_id": reply_id,
        "real_id": reply_id,
        "sender": {"user_id": _USER_ID, "nickname": "群友", "card": ""},
        "message": [{"type": "text", "data": {"text": "补取到的引用原文"}}],
        "group_id": group_id,
    }


def _summon_with_incomplete_reply(
    world: _World,
    *,
    group_id: int,
    reply_id: int,
) -> GroupMessageEvent:
    """原生 @ 本体 + 原生 reply（已有但正文不完整）的一次召唤事件。"""
    world.bot.get_msg_payload = _refetch_payload(reply_id=reply_id, group_id=group_id)
    return world.event(
        segments=(
            MessageSegment.at(_BOT_SELF_ID),
            MessageSegment.reply(reply_id),
            " 接着说",
        ),
        reply=_incomplete_reply(reply_id=reply_id),
    )


async def test_successful_first_delivery_resolves_reply_context_after_claim(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首次成功：交接成功后补取生成上下文一次，且不请求模型/引用链。"""
    group_id = 700_000 + uuid4().int % 100_000
    reply_id = 555_301
    note_scope("onebot_v11", str(_BOT_SELF_ID), str(group_id))
    async with _world(monkeypatch, decision_database) as (world, harness):
        event = _summon_with_incomplete_reply(
            world, group_id=group_id, reply_id=reply_id
        )

        await _dispatch(world, event)

        assert len(world.get_msg_calls) == 1
        assert harness.transport.requests == []
        assert harness.reference_calls == []
        assert len(world.fulfillment.pending) == 1
        assert len(world.generation_kwargs) == 1
        generation = world.generation_kwargs[0]
        assert generation["reply_context_requested"] is True
        assert generation["reply_context_refetched"] is True
        context = generation["reply_context"]
        assert context is not None
        assert context.text == "补取到的引用原文"
    note_scope("onebot_v11", str(_BOT_SELF_ID), str(group_id))


async def test_redelivery_never_makes_generation_context_get_msg_calls(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一 Event 对象平台重投：不重新取得交接，也不补取生成上下文。"""
    group_id = 700_000 + uuid4().int % 100_000
    reply_id = 555_302
    note_scope("onebot_v11", str(_BOT_SELF_ID), str(group_id))
    async with _world(monkeypatch, decision_database) as (world, harness):
        event = _summon_with_incomplete_reply(
            world, group_id=group_id, reply_id=reply_id
        )
        await _dispatch(world, event)
        assert len(world.get_msg_calls) == 1

        await _dispatch(world, event)

        assert len(world.get_msg_calls) == 1, "外部重投仍补取了生成上下文"
        assert len(harness.transport.requests) == 0
        assert len(world.fulfillment.pending) == 1
        assert len(world.generation_kwargs) == 1
    note_scope("onebot_v11", str(_BOT_SELF_ID), str(group_id))


async def test_refused_handoff_never_makes_generation_context_get_msg_calls(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """判定成功但交接被拒（撤权）：不补取生成上下文、不生成回复。"""
    group_id = 700_000 + uuid4().int % 100_000
    reply_id = 555_303
    note_scope("onebot_v11", str(_BOT_SELF_ID), str(group_id))
    admitted = {"value": True}
    options = HarnessOptions(admission=lambda _identity: admitted["value"])
    async with _world(monkeypatch, decision_database, options=options) as (
        world,
        harness,
    ):
        warmup = _summon_with_incomplete_reply(
            world, group_id=group_id, reply_id=reply_id
        )
        await _dispatch(world, warmup)
        assert len(world.get_msg_calls) == 1
        assert len(world.memory.retain_calls) == 1

        # 判定已完成、既有履约重复检查之后撤权：交接必然被拒。
        def _revoke_after_evaluation() -> None:
            admitted["value"] = False

        world.fulfillment.on_duplicate_check = _revoke_after_evaluation
        refused = _summon_with_incomplete_reply(
            world, group_id=group_id, reply_id=reply_id
        )
        refused_message_id = refused.message_id

        await _dispatch(world, refused)

        assert len(world.get_msg_calls) == 1, "交接被拒后仍补取了生成上下文"
        assert len(world.memory.retain_calls) == 1
        assert len(world.fulfillment.pending) == 1
        assert len(world.generation_kwargs) == 1
        row = await _dispatch_row(harness, world, refused)
        assert row is not None
        assert row["state"] == "cancelled"
        assert row["reason_code"] == "admission_revoked"
        assert row["handed_off_at"] is None
        assert refused_message_id > warmup.message_id
    note_scope("onebot_v11", str(_BOT_SELF_ID), str(group_id))


@pytest.mark.parametrize("route", ["chat", "summary"])
async def test_completed_result_is_fenced_by_persistent_disable_and_stays_fenced(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    """持久停用栅栏：已完成结果在停用后拒绝交接，持久重启启用后仍然拒绝。

    使用真实迁移定义 ``komari_decision_disable_fence``：提交
    ``komari_decision_config.plugin_enable = false`` 后，未交接的成功行被改为
    ``cancelled/disabled`` 并推进调度代次；恢复 ``plugin_enable = true`` 不会
    还原行状态或代次。对照组证明同一路径在未停用时可以正常领取。
    """
    options = _summary_options() if route == "summary" else HarnessOptions()
    async with _world(monkeypatch, decision_database, options=options) as (
        world,
        harness,
    ):
        assert harness.decision is not None
        assert harness.engine is not None

        # 对照组：未停用时同一路由可以成功领取一次。
        control_event = world.event(
            text=_SUMMARY_TEXT if route == "summary" else _NORMAL_TEXT,
            to_me=route == "summary",
        )
        control = await _carrier(world, control_event)
        control_result = await control.evaluate(
            harness.decision, summary_candidate=route == "summary"
        )
        assert control_result.route == route
        assert await control.claim_handoff(cast("Any", route)) is True

        # 被测分发：判定成功完成，但尚未交接。
        event = world.event(
            text=_SUMMARY_TEXT if route == "summary" else _NORMAL_TEXT,
            to_me=route == "summary",
        )
        dispatch = await _carrier(world, event)
        result = await dispatch.evaluate(
            harness.decision, summary_candidate=route == "summary"
        )
        assert result.route == route
        assert result.status == "success"

        # 持久停用（真实 PG 提交，触发迁移 0023 的停用栅栏）。
        await update_config_row(harness.engine, {"plugin_enable": False})
        row = await _dispatch_row(harness, world, event)
        assert row is not None
        assert row["state"] == "cancelled"
        assert row["reason_code"] == "disabled"
        assert row["business_route"] == "none"
        assert await dispatch.claim_handoff(cast("Any", route)) is False

        # 持久重新启用：已撤销的未交接结果不会复活。
        await update_config_row(harness.engine, {"plugin_enable": True})
        assert await dispatch.claim_handoff(cast("Any", route)) is False
        row_after = await _dispatch_row(harness, world, event)
        assert row_after is not None
        assert row_after["handed_off_at"] is None


# ---------------------------------------------------------------------------
# TSK-188 判定失败通知：固定群提示、私聊诊断、冷却、开关与安全投影
# ---------------------------------------------------------------------------

_CANARY = "TSK188CANARY"
_CANARY_TEXT = "值班 sk-TSK188CANARY1 https://c.invalid"
_CANARY_ERROR = (
    "api_key=TSK188CANARY https://canary.invalid/p [CQ:image,file=TSK188CANARY.jpg]"
)


def _exception_options(*exceptions: BaseException) -> HarnessOptions:
    """按请求序号抛出预先给定的外部传输异常（其余判定语义保持真实）。"""

    def _raise(_request: httpx2.Request, index: int) -> Any:
        raise exceptions[min(index, len(exceptions) - 1)]

    return HarnessOptions(responder=_raise)


def _api_calls(world: _World, api: str) -> list[dict[str, Any]]:
    """按 OneBot API 名读取真实共享通知器发出的最终平台载荷。"""
    return [
        cast("dict[str, Any]", data) for name, data in world.bot.calls if name == api
    ]


def _generic_card(
    *,
    task_kind: str,
    group_id: int,
    reason_code: str,
    decision_id: str,
) -> str:
    """共享通知器的 generic 诊断卡：只含白名单字段（无摘要/正文/异常）。"""
    return "\n".join(
        (
            f"任务: {task_kind}",
            f"群: {group_id}",
            "阶段: decision",
            f"原因: {reason_code}",
            f"trace: {decision_id}",
        )
    )


_NOTIFICATION_LOG_TAGS = ("[GroupHistorySummary]", "[KomariChat]")


def _module_logs(messages: list[Any]) -> list[str]:
    """只保留本票通知路径的模块日志（不含第三方/引擎日志）。"""
    return [
        str(message)
        for message in messages
        if any(tag in str(message) for tag in _NOTIFICATION_LOG_TAGS)
    ]


@contextmanager
def _capture_logs() -> Iterator[list[Any]]:
    """捕获 loguru 日志（``nonebot.logger`` 就是 loguru 全局 logger）。"""
    messages: list[Any] = []

    def _sink(message: Any) -> None:
        messages.append(message)

    handler_id = loguru_logger.add(_sink, level="DEBUG")
    try:
        yield messages
    finally:
        loguru_logger.remove(handler_id)


def _enable_reaction(world: _World) -> None:
    """开启生成中表情，证明失败路径的固定群提示不依赖（也不伪造）表情。"""
    assert world.memory_config is not None
    world.memory_config.face_reaction_enabled = True
    world.memory_config.face_reaction_id = "1"


async def test_summary_decision_failure_sends_only_fixed_group_text(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """总结 Noul 技术失败：固定群提示一次，不生成、不总结、不贴表情、不刷私聊。"""
    options = _exception_options(httpx2.ConnectError(_TRANSPORT_DOWN))
    async with _world(monkeypatch, decision_database, options=options) as (
        world,
        harness,
    ):
        _enable_reaction(world)
        event = world.event(text=_SUMMARY_TEXT, to_me=True)

        await _dispatch(world, event)

        assert len(harness.transport.requests) == 1
        assert _api_calls(world, "send_group_msg") == [
            {
                "group_id": world.group_id,
                "message": [
                    {"type": "reply", "data": {"id": str(event.message_id)}},
                    {
                        "type": "text",
                        "data": {"text": summary_module.CLASSIFICATION_FAILURE_MESSAGE},
                    },
                ],
            }
        ]
        # 固定群提示不依赖生成中表情，也绝不伪造 reaction_sent。
        assert _api_calls(world, "set_msg_emoji_like") == []
        assert _api_calls(world, "send_private_msg") == []
        assert world.bot.sent == []
        assert world.summary_executions == []
        assert world.generation_kwargs == []
        assert world.fulfillment.pending == []
        assert world.redis.pushed == []
        row = await _dispatch_row(harness, world, event)
        assert row is not None
        assert row["state"] == "failed"
        assert row["reason_code"] == "network_error"
        assert row["business_route"] == "none"
        assert row["notification_eligible"] is True
        assert row["notified_at"] is not None
        assert row["handed_off_at"] is None


async def test_summary_decision_failure_private_card_is_safe_and_exact(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """总结失败的私聊诊断只含白名单字段；正文/异常/媒体/凭据金丝雀不泄漏。"""
    options = _exception_options(httpx2.ConnectError(_CANARY_ERROR))
    logs: list[Any]
    with _capture_logs() as logs:
        async with _world(
            monkeypatch, decision_database, options=options, notify=True
        ) as (world, harness):
            event = world.event(text=f"{_SUMMARY_TEXT} sk-{_CANARY}", to_me=True)

            await _dispatch(world, event)

            row = await _dispatch_row(harness, world, event)
            assert row is not None
            assert row["reason_code"] == "network_error"
            private = _api_calls(world, "send_private_msg")
            assert private == [
                {
                    "user_id": 10001,
                    "message": _generic_card(
                        task_kind="group_history_summary",
                        group_id=world.group_id,
                        reason_code="network_error",
                        decision_id=str(row["decision_id"]),
                    ),
                }
            ]
            # 固定群提示仍然保留（不因私聊开关/冷却而不发）。
            assert len(_api_calls(world, "send_group_msg")) == 1
            for _api, data in world.bot.calls:
                assert _CANARY not in str(data), "最终通知载荷泄漏了金丝雀"
    module_logs = _module_logs(logs)
    assert module_logs, "通知路径必须留下可审计的安全诊断日志"
    for line in module_logs:
        assert _CANARY not in line, "普通运行日志泄漏了金丝雀"


async def test_ordinary_decision_failure_is_private_only_and_keeps_original_message(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """普通判定技术失败：不发群错误、不生成，原消息继续既有保存处理。"""
    options = _exception_options(httpx2.ConnectError(_CANARY_ERROR))
    async with _world(monkeypatch, decision_database, options=options, notify=True) as (
        world,
        harness,
    ):
        _enable_reaction(world)
        event = world.event(text=_CANARY_TEXT)

        await _dispatch(world, event)

        assert _api_calls(world, "send_group_msg") == []
        assert world.bot.sent == []
        assert _api_calls(world, "set_msg_emoji_like") == []
        assert world.generation_kwargs == []
        assert world.fulfillment.pending == []
        assert len(world.redis.pushed) == 1
        assert world.redis.pushed[0].content == _CANARY_TEXT
        row = await _dispatch_row(harness, world, event)
        assert row is not None
        assert row["state"] == "failed"
        assert row["reason_code"] == "network_error"
        assert row["notified_at"] is not None
        private = _api_calls(world, "send_private_msg")
        assert private == [
            {
                "user_id": 10001,
                "message": _generic_card(
                    task_kind="chat_reply",
                    group_id=world.group_id,
                    reason_code="network_error",
                    decision_id=str(row["decision_id"]),
                ),
            }
        ]
        for _api, data in world.bot.calls:
            assert _CANARY not in str(data), "最终通知载荷泄漏了金丝雀"
        # 既有 Redis 冷却客户端被真实使用（SET NX EX），不是静默空操作。
        set_calls = world.redis.redis.set_calls
        assert len(set_calls) == 1
        key, _value, nx, ex = set_calls[0]
        assert key.startswith("komari:onebot:group_task_failure:")
        assert nx is True
        assert ex == 300

        # 平台重投同样不得重复提交私聊失败诊断。
        await _dispatch(world, event)
        assert len(harness.transport.requests) == 1
        assert len(_api_calls(world, "send_private_msg")) == 1
        assert len(world.redis.redis.set_calls) == 1


async def test_error_switch_and_superusers_are_read_per_failure(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """每次失败都重读 error_notify_enabled 并重新解析 SUPERUSER 收件人。"""
    options = _exception_options(
        httpx2.ConnectError(_CANARY_ERROR),
        httpx2.ConnectTimeout(_CANARY_ERROR),
    )
    async with _world(
        monkeypatch,
        decision_database,
        options=options,
        notify=False,
        superusers={"10001", "不是号码", "-7"},
    ) as (world, harness):
        assert world.memory_config is not None
        first = world.event(text=_SUMMARY_TEXT, to_me=True)
        await _dispatch(world, first)

        # 私聊开关关闭：只剩固定群提示，非法 SUPERUSER 也不产生任何投递。
        assert len(_api_calls(world, "send_group_msg")) == 1
        assert _api_calls(world, "send_private_msg") == []

        world.memory_config.error_notify_enabled = True
        world.superusers.clear()
        world.superusers.update({"10002"})
        second = world.event(text=_SUMMARY_TEXT, to_me=True)
        await _dispatch(world, second)

        assert len(_api_calls(world, "send_group_msg")) == 2
        private = _api_calls(world, "send_private_msg")
        assert len(private) == 1
        assert private[0]["user_id"] == 10002
        row = await _dispatch_row(harness, world, second)
        assert row is not None
        assert row["reason_code"] == "request_timeout"
        assert "原因: request_timeout" in str(private[0]["message"])
        assert harness.transport.requests


async def test_successive_summary_failures_share_private_cooldown_but_keep_group_text(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同群同原因的连续新失败共享私聊冷却，但群内固定提示每次都发。"""
    options = _exception_options(httpx2.ConnectError(_TRANSPORT_DOWN))
    async with _world(monkeypatch, decision_database, options=options, notify=True) as (
        world,
        harness,
    ):
        first = world.event(text=_SUMMARY_TEXT, to_me=True)
        second = world.event(text=_SUMMARY_TEXT, to_me=True)

        await _dispatch(world, first)
        await _dispatch(world, second)

        assert len(harness.transport.requests) == 2
        assert len(_api_calls(world, "send_group_msg")) == 2
        assert len(_api_calls(world, "send_private_msg")) == 1
        for event in (first, second):
            row = await _dispatch_row(harness, world, event)
            assert row is not None
            assert row["notified_at"] is not None


async def test_successive_ordinary_failures_share_redis_cooldown(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """普通判定失败复用既有 Redis 冷却：新事件两次尝试只投递一张私聊卡。"""
    options = _exception_options(httpx2.ConnectError(_TRANSPORT_DOWN))
    async with _world(monkeypatch, decision_database, options=options, notify=True) as (
        world,
        harness,
    ):
        await _dispatch(world, world.event(text=_NORMAL_TEXT))
        await _dispatch(world, world.event(text=_NORMAL_TEXT))

        assert len(harness.transport.requests) == 2
        assert len(_api_calls(world, "send_group_msg")) == 0
        assert len(_api_calls(world, "send_private_msg")) == 1
        assert len(world.redis.redis.set_calls) == 2
        assert len(world.redis.redis.keys) == 1


async def test_platform_redelivery_does_not_resubmit_failure_notifications(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """平台重投不得重复提交群内固定提示或私聊失败诊断。"""
    options = _exception_options(httpx2.ConnectError(_TRANSPORT_DOWN))
    async with _world(monkeypatch, decision_database, options=options, notify=True) as (
        world,
        harness,
    ):
        event = world.event(text=_SUMMARY_TEXT, to_me=True)

        await _dispatch(world, event)
        await _dispatch(world, event)  # 同一个 Event 对象再次分发

        assert len(harness.transport.requests) == 1
        assert len(_api_calls(world, "send_group_msg")) == 1
        assert len(_api_calls(world, "send_private_msg")) == 1
        row = await _dispatch_row(harness, world, event)
        assert row is not None
        assert row["notified_at"] is not None
        assert row["handed_off_at"] is None


async def test_failure_notification_is_not_revived_by_persistent_disable_reenable(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """持久停用会撤销未提交的失败通知资格；重新启用不会复活。"""
    options = _exception_options(httpx2.ConnectError(_TRANSPORT_DOWN))
    async with _world(monkeypatch, decision_database, options=options) as (
        world,
        harness,
    ):
        assert harness.decision is not None and harness.engine is not None
        event = world.event(text=_NORMAL_TEXT)
        dispatch = await _carrier(world, event)
        result = await dispatch.evaluate(harness.decision, summary_candidate=False)
        assert result.status == "failed"
        assert result.reason_code == "network_error"
        row = await _dispatch_row(harness, world, event)
        assert row is not None
        assert row["notification_eligible"] is True
        assert row["notified_at"] is None

        await update_config_row(harness.engine, {"plugin_enable": False})
        fenced = await _dispatch_row(harness, world, event)
        assert fenced is not None
        assert fenced["state"] == "cancelled"
        assert fenced["reason_code"] == "disabled"
        assert fenced["notification_eligible"] is False
        assert await dispatch.claim_handoff("failure_notification") is False

        await update_config_row(harness.engine, {"plugin_enable": True})
        assert await dispatch.claim_handoff("failure_notification") is False
        assert world.bot.calls == []


async def test_failure_notification_is_not_revived_by_admission_restore(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """观察到的撤权会持久化；恢复准入后失败的迟到结果也不能补交通知。"""
    admitted = {"value": True}

    def _fail(_request: httpx2.Request, _index: int) -> Any:
        raise httpx2.ConnectError(_TRANSPORT_DOWN)

    options = HarnessOptions(
        responder=_fail,
        admission=lambda _identity: admitted["value"],
    )
    async with _world(monkeypatch, decision_database, options=options) as (
        world,
        harness,
    ):
        assert harness.decision is not None
        event = world.event(text=_NORMAL_TEXT)
        dispatch = await _carrier(world, event)
        result = await dispatch.evaluate(harness.decision, summary_candidate=False)
        assert result.status == "failed"
        assert result.reason_code == "network_error"
        row = await _dispatch_row(harness, world, event)
        assert row is not None
        assert row["notification_eligible"] is True

        admitted["value"] = False
        assert await dispatch.claim_handoff("failure_notification") is False
        revoked = await _dispatch_row(harness, world, event)
        assert revoked is not None
        assert revoked["state"] == "cancelled"
        assert revoked["reason_code"] == "admission_revoked"

        admitted["value"] = True
        assert await dispatch.claim_handoff("failure_notification") is False
        assert await dispatch.claim_handoff("failure_notification") is False
        assert world.bot.calls == []


# ---------------------------------------------------------------------------
# TSK-183 判定边界异常隔离：普通异常不外泄、不授权通知、不落入第二条业务路径
# ---------------------------------------------------------------------------

_BOUNDARY_CANARY = "TSK183BOUNDARYCANARY"
_BOUNDARY_ERROR_TEXT = (
    f"{_BOUNDARY_CANARY} api_key={_BOUNDARY_CANARY} https://canary.invalid/p"
)


class _BoundaryMatcher:
    """直接边界测试用当前 matcher 替身：只记录「停止传播」事实。"""

    def __init__(self) -> None:
        self.block = False

    def stop_propagation(self) -> None:
        self.block = True


def _observe_engine_results(
    monkeypatch: pytest.MonkeyPatch,
    harness: DecisionHarness,
) -> list[DecisionResult]:
    """观察真实引擎产出的类型化结果（委托原实现，不替换判定语义）。"""
    engine = harness.decision
    assert engine is not None
    original = engine.evaluate
    results: list[DecisionResult] = []

    async def _evaluate(request: DecisionRequest) -> DecisionResult:
        result = await original(request)
        results.append(result)
        return result

    monkeypatch.setattr(engine, "evaluate", _evaluate)
    return results


def _raise_at_engine_evaluate(
    monkeypatch: pytest.MonkeyPatch,
    harness: DecisionHarness,
    *,
    error: BaseException,
) -> None:
    """在真实引擎的评估边界注入异常（不替换调用方、不伪造判定结果）。"""
    engine = harness.decision
    assert engine is not None

    async def _evaluate(_request: DecisionRequest) -> DecisionResult:
        raise error

    monkeypatch.setattr(engine, "evaluate", _evaluate)


def _raise_at_engine_handoff(
    monkeypatch: pytest.MonkeyPatch,
    harness: DecisionHarness,
    *,
    route: str,
    error: BaseException,
    on_call: Callable[[], None] | None = None,
) -> list[str]:
    """在真实引擎的交接边界按路由注入异常（其余路由仍走真实实现）。"""
    engine = harness.decision
    assert engine is not None
    original = engine.claim_handoff
    reached: list[str] = []

    async def _claim(
        request: DecisionRequest,
        result: DecisionResult,
        handed_route: HandoffRoute,
    ) -> bool:
        if handed_route != route:
            return bool(await original(request, result, handed_route))
        reached.append(handed_route)
        if on_call is not None:
            on_call()
        raise error

    monkeypatch.setattr(engine, "claim_handoff", _claim)
    return reached


def _record_failure_report(world: _World) -> list[dict[str, Any]]:
    """记录入口失败善后调用，但**不抑制**原始行为（通知/群文本照常发生）。"""
    handler = world.entry._get_or_build_handler()
    calls: list[dict[str, Any]] = []
    original = handler.report_reply_failure

    async def _record(
        *,
        bot: object,
        event: object,
        failure: object,
        reason: object,
    ) -> None:
        recorded = cast("Any", failure)
        calls.append(
            {
                "stage": recorded.stage,
                "error_type": recorded.error_type,
                "summary": recorded.summary,
            }
        )
        await original(bot=bot, event=event, failure=failure, reason=reason)

    handler.report_reply_failure = _record  # type: ignore[method-assign]
    return calls


def _assert_no_boundary_effects(world: _World) -> None:
    """被隔离的判定边界之后：没有生成、总结、表情、履约、平台读取或发送。"""
    assert world.generation_kwargs == []
    assert world.summary_executions == []
    assert world.fulfillment.pending == []
    assert world.reservation.calls == []
    assert world.get_msg_calls == []
    assert _api_calls(world, "set_msg_emoji_like") == []
    assert world.bot.sent == []


async def test_chat_evaluation_exception_is_isolated_from_failure_notification(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """普通异常发生在引擎评估边界：不外泄到入口失败善后，也不授权诊断。"""
    async with _world(monkeypatch, decision_database, notify=True) as (world, harness):
        _enable_reaction(world)
        _raise_at_engine_evaluate(
            monkeypatch, harness, error=RuntimeError(_BOUNDARY_ERROR_TEXT)
        )
        reports = _record_failure_report(world)
        event = world.event(text=f"{_NORMAL_TEXT} sk-{_BOUNDARY_CANARY}")

        with _capture_logs() as logs:
            await _dispatch(world, event)

        # 失败善后与其私聊/群内诊断绝不能被判定边界异常触发。
        assert reports == []
        assert world.bot.calls == []
        assert harness.transport.requests == []
        _assert_no_boundary_effects(world)
        # 抛出的评估没有有效结果：不保存原消息、不产生任何业务副作用。
        assert world.redis.pushed == []
        for line in _module_logs(logs):
            assert _BOUNDARY_CANARY not in line, "边界异常正文泄漏到普通日志"


async def test_chat_handoff_exception_is_isolated_from_failure_notification(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """普通异常发生在聊天交接边界：不补取生成上下文，也不授权任何诊断。"""
    async with _world(monkeypatch, decision_database, notify=True) as (world, harness):
        _enable_reaction(world)
        reached = _raise_at_engine_handoff(
            monkeypatch,
            harness,
            route="chat",
            error=RuntimeError(_BOUNDARY_ERROR_TEXT),
        )
        reports = _record_failure_report(world)
        event = _summon_with_incomplete_reply(
            world, group_id=world.group_id, reply_id=555_401
        )

        with _capture_logs() as logs:
            await _dispatch(world, event)

        assert reached == ["chat"]
        assert reports == []
        assert world.bot.calls == []
        _assert_no_boundary_effects(world)
        # 交接抛出发生在引用补取之前：绝不发起生成上下文的平台读取。
        assert world.get_msg_calls == []
        assert world.redis.pushed == []
        for line in _module_logs(logs):
            assert _BOUNDARY_CANARY not in line, "边界异常正文泄漏到普通日志"


async def test_summary_handoff_exception_keeps_both_paths_stopped(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """命中总结的交接抛出：绝不落穿到普通聊天写记忆（memory_candidate=True）。"""
    async with _world(
        monkeypatch, decision_database, options=_summary_options(), notify=True
    ) as (world, harness):
        _enable_reaction(world)
        results = _observe_engine_results(monkeypatch, harness)
        reached = _raise_at_engine_handoff(
            monkeypatch,
            harness,
            route="summary",
            error=RuntimeError(_BOUNDARY_ERROR_TEXT),
        )
        event = world.event(text=f"{_SUMMARY_TEXT} sk-{_BOUNDARY_CANARY}", to_me=True)

        with _capture_logs() as logs:
            await _dispatch(world, event)

        assert reached == ["summary"]
        # 被测场景正是「命中的总结结果带 memory_candidate=True」。
        assert [(result.route, result.memory_candidate) for result in results] == [
            ("summary", True)
        ]
        row = await _dispatch_row(harness, world, event)
        assert row is not None
        assert row["state"] == "success"
        assert row["business_route"] == "summary"
        assert row["handed_off_at"] is None
        # 未取得总结交接资格：普通聊天不得接手，绝不写记忆或执行总结。
        assert world.redis.pushed == []
        _assert_no_boundary_effects(world)
        assert world.bot.calls == []
        for line in _module_logs(logs):
            assert _BOUNDARY_CANARY not in line, "边界异常正文泄漏到普通日志"


async def test_chat_evaluation_boundary_still_propagates_cancellation(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """直接边界：评估被取消时 CancelledError 继续向上传播（不转成安全停止）。"""
    async with _world(monkeypatch, decision_database) as (world, harness):
        handler = world.entry._get_or_build_handler()
        _raise_at_engine_evaluate(monkeypatch, harness, error=asyncio.CancelledError())
        event = world.event(text=_NORMAL_TEXT)
        dispatch = await _carrier(world, event)

        with pytest.raises(asyncio.CancelledError):
            await handler.process_message(
                cast("Any", world.bot),
                cast("Any", event),
                decision_dispatch=dispatch,
                reply_allowed=True,
            )

        _assert_no_boundary_effects(world)
        assert world.redis.pushed == []


async def test_chat_handoff_boundary_still_propagates_cancellation(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """直接边界：聊天交接被取消时 CancelledError 继续向上传播。"""
    async with _world(monkeypatch, decision_database) as (world, harness):
        handler = world.entry._get_or_build_handler()
        _raise_at_engine_handoff(
            monkeypatch,
            harness,
            route="chat",
            error=asyncio.CancelledError(),
        )
        event = world.event(text=_NORMAL_TEXT)
        dispatch = await _carrier(world, event)

        with pytest.raises(asyncio.CancelledError):
            await handler.process_message(
                cast("Any", world.bot),
                cast("Any", event),
                decision_dispatch=dispatch,
                reply_allowed=True,
            )

        _assert_no_boundary_effects(world)
        assert world.redis.pushed == []


async def test_summary_handoff_cancellation_keeps_propagation_stopped(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """直接边界：总结交接被取消时传播已停止，且 CancelledError 继续传播。"""
    async with _world(monkeypatch, decision_database, options=_summary_options()) as (
        world,
        harness,
    ):
        matcher = _BoundaryMatcher()
        blocked_at_claim: list[bool] = []
        _raise_at_engine_handoff(
            monkeypatch,
            harness,
            route="summary",
            error=asyncio.CancelledError(),
            on_call=lambda: blocked_at_claim.append(matcher.block),
        )
        event = world.event(text=_SUMMARY_TEXT, to_me=True)
        state: dict[Any, Any] = {}
        await _prepare_state(world, event, state)

        token = summary_module.current_matcher.set(cast("Any", matcher))
        try:
            with pytest.raises(asyncio.CancelledError):
                await summary_module.handle_group_history_summary(
                    cast("Any", world.bot), cast("Any", event), state
                )
        finally:
            summary_module.current_matcher.reset(token)

        # 传播在总结交接 await 之前就已停止；取消不会重新开放普通聊天。
        assert blocked_at_claim == [True]
        assert matcher.block is True
        assert world.summary_executions == []
        _assert_no_boundary_effects(world)
        assert world.redis.pushed == []
