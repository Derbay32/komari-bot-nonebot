"""TSK-224 tracer slice2: 事件门控矩阵验收（生产依赖）。

覆盖 5 种事件门控场景，通过公共遥测 / trace / Bot 调用观察：

1. PrivateMessage 被拒绝且遥测记 private_input_rejected
2. 8 种无归属参数事件，各记 group_attribution_unavailable
3. 5 种畸形 GroupMessage group_id，故障关闭 + 同族 message
4. MetaEvent 三种变体全部通过，遥测保持不变
5. FAILED 运行时有效 GroupMessage 被 effective_policy_unavailable 拒绝

每个用例创建 ``prepare_control_plane``，进入 ``event_gate_context``，
注册对应探针，分发事件，读取同一 app 的 status。无私有生产性断言、
catch/skip/sleep。

当前生产缺少 ``event_gate`` 模块：私有消息/无归属/畸形/失败运行时
产生非空 trace（未拦截），MetaEvent 正常通过（trace 完整）。这就是
有意义的 RED 失败。Slice1 受限测试（test_event_gate_flow.py）不变。
"""

from __future__ import annotations

from typing import Any

import pytest
from nonebot.adapters.onebot.v11.event import (
    Event,
    FriendAddNoticeEvent,
    FriendRecallNoticeEvent,
    FriendRequestEvent,
    GroupMessageEvent,
    HeartbeatMetaEvent,
    LifecycleMetaEvent,
    MessageEvent,
    MetaEvent,
    NoticeEvent,
    PokeNotifyEvent,
    PrivateMessageEvent,
    RequestEvent,
)

from tests.group_admission.entry_gate_support import (
    ProbeBot,
    dispatch,
    event_gate_context,
    make_v11_event,
    read_status,
    register_phase_probe,
)
from tests.group_admission.management_support import (
    prepare_control_plane,
)
from tests.group_admission.observability_support import (
    FakeUtcClock,
    build_runtime_kwargs,
)
from tests.group_admission.runtime_support import (
    AdmissionStorageFake,
    stored_policy,
)

pytestmark = pytest.mark.group_admission_acceptance

_POLICY = {"mode": "blacklist", "group_ids": []}


async def _base_env(
    monkeypatch: pytest.MonkeyPatch, *, fetch_error: Exception | None = None
) -> tuple[Any, Any, Any]:
    """创建基础环境：存储 + 控制面 + 假时钟。

    返回 ``(app, runtime, manager)``。
    """
    clock = FakeUtcClock()
    storage = AdmissionStorageFake(
        stored_policy(1, _POLICY), fetch_error=fetch_error
    )
    return await prepare_control_plane(
        monkeypatch,
        storage,
        runtime_kwargs=build_runtime_kwargs(clock=clock),
    )


async def _dispatch_and_read(
    app: Any,
    event: Event,
    event_family: str,
) -> tuple[list[str], dict[str, Any], ProbeBot]:
    """进入门控上下文、注册探针、分发、读取 status 后返回 ``(trace, telemetry, bot)``。"""
    bot = ProbeBot()
    trace: list[str] = []
    async with event_gate_context():
        register_phase_probe(trace, event_family)
        await dispatch(bot, event)
    body = await read_status(app)
    return trace, body["telemetry"], bot


# ---------------------------------------------------------------------------
# Case 1: PrivateMessage 只有遥测 private_input_rejected
# ---------------------------------------------------------------------------


async def test_private_message_rejected_with_private_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PrivateMessage 被门控拒绝：trace 空、bot 零、遥测 private_input_rejected=1。"""
    app, _rt, _mgr = await _base_env(monkeypatch)
    event = make_v11_event(PrivateMessageEvent, post_type="message", message_type="private")
    trace, telemetry, bot = await _dispatch_and_read(app, event, "message")
    assert trace == [], f"期望拦截（空 trace），实际 {trace}"
    assert bot.calls == [], f"bot.calls={bot.calls}"
    assert telemetry["adjudications_total"] == 1
    assert telemetry["by_reason_code"]["private_input_rejected"] == 1
    assert telemetry["attribution_failures_by_event_family"]["message"] == 0


# ---------------------------------------------------------------------------
# Case 2: 7 种无归属参数事件，各记 group_attribution_unavailable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event_cls", "overrides", "event_family", "expected_family"),
    [
        (FriendRequestEvent, {"post_type": "request", "request_type": "friend"}, "request", "request"),
        (FriendAddNoticeEvent, {"post_type": "notice", "notice_type": "friend_add"}, "notice", "notice"),
        (FriendRecallNoticeEvent, {"post_type": "notice", "notice_type": "friend_recall"}, "notice", "notice"),
        (PokeNotifyEvent, {"post_type": "notice", "notice_type": "notify", "group_id": None}, "notice", "notice"),
        (MessageEvent, {"post_type": "message"}, "message", "message"),
        (NoticeEvent, {"post_type": "notice"}, "notice", "notice"),
        (RequestEvent, {"post_type": "request"}, "request", "request"),
        (Event, {"post_type": "unknown"}, "unknown", "unknown"),
    ],
    ids=[
        "friend-request",
        "friend-add-notice",
        "friend-recall-notice",
        "poke-notify-group-none",
        "base-message-event",
        "base-notice-event",
        "base-request-event",
        "base-event-unknown",
    ],
)
async def test_no_attribution_events_rejected(
    monkeypatch: pytest.MonkeyPatch,
    event_cls: type[Event],
    overrides: dict[str, object],
    event_family: str,
    expected_family: str,
) -> None:
    """无归属参数事件被拒绝：trace 空、bot 0、遥测 group_attribution_unavailable。"""
    app, _rt, _mgr = await _base_env(monkeypatch)
    event = make_v11_event(event_cls, **overrides)
    trace, telemetry, bot = await _dispatch_and_read(app, event, event_family)
    assert trace == [], f"期望拦截（空 trace），实际 {trace}"
    assert bot.calls == [], f"bot.calls={bot.calls}"
    assert telemetry["adjudications_total"] == 1
    assert telemetry["by_reason_code"]["group_attribution_unavailable"] == 1
    assert telemetry["attribution_failures_by_event_family"][expected_family] == 1


# ---------------------------------------------------------------------------
# Case 3: 畸形 GroupMessage group_id，故障关闭 + 同族 message
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_group_id",
    [None, 0, -1, True, "100"],
    ids=["group-none", "group-zero", "group-negative", "group-bool", "group-string"],
)
async def test_malformed_group_id_rejected(
    monkeypatch: pytest.MonkeyPatch,
    bad_group_id: Any,
) -> None:
    """畸形 group_id 故障关闭：trace 空、bot 0、遥测 message 1。"""
    app, _rt, _mgr = await _base_env(monkeypatch)
    event = make_v11_event(GroupMessageEvent, group_id=bad_group_id)
    trace, telemetry, bot = await _dispatch_and_read(app, event, "message")
    assert trace == [], f"期望拦截（空 trace），实际 {trace}"
    assert bot.calls == [], f"bot.calls={bot.calls}"
    assert telemetry["adjudications_total"] == 1
    assert telemetry["by_reason_code"]["group_attribution_unavailable"] == 1
    assert telemetry["attribution_failures_by_event_family"]["message"] == 1


# ---------------------------------------------------------------------------
# Case 4: MetaEvent 三种变体全部通过，遥测不变
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event_cls", "overrides"),
    [
        (MetaEvent, {"post_type": "meta_event", "meta_event_type": ""}),
        (LifecycleMetaEvent, {"post_type": "meta_event", "meta_event_type": "lifecycle"}),
        (HeartbeatMetaEvent, {"post_type": "meta_event", "meta_event_type": "heartbeat", "status": None, "interval": 0}),
    ],
    ids=["base-meta", "lifecycle", "heartbeat"],
)
async def test_meta_events_pass_through(
    monkeypatch: pytest.MonkeyPatch,
    event_cls: type[Event],
    overrides: dict[str, object],
) -> None:
    """MetaEvent 三种变体全部通过：trace 完整五阶段、遥测 total=0。"""
    app, _rt, _mgr = await _base_env(monkeypatch)
    event = make_v11_event(event_cls, **overrides)
    trace, telemetry, bot = await _dispatch_and_read(app, event, "meta_event")
    assert bot.calls == [], f"bot.calls={bot.calls}"
    assert trace == [
        "rule",
        "run_pre",
        "handler",
        "run_post",
        "event_post",
    ], f"期望完整五阶段，实际 {trace}"
    assert telemetry["adjudications_total"] == 0


# ---------------------------------------------------------------------------
# Case 5: FAILED 运行时有效 GroupMessage 被 effective_policy_unavailable 拒绝
# ---------------------------------------------------------------------------


async def test_failed_runtime_rejects_group_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAILED 运行时有效 GroupMessage 被拒绝：trace 空、遥测。"""
    app, _rt, _mgr = await _base_env(
        monkeypatch, fetch_error=RuntimeError("storage offline")
    )
    event = make_v11_event(GroupMessageEvent, group_id=100)
    trace, telemetry, bot = await _dispatch_and_read(app, event, "message")
    assert trace == [], f"期望拦截（空 trace），实际 {trace}"
    assert bot.calls == [], f"bot.calls={bot.calls}"
    assert telemetry["adjudications_total"] == 1
    assert telemetry["by_reason_code"]["effective_policy_unavailable"] == 1
    assert telemetry["attribution_failures_by_event_family"]["message"] == 0
