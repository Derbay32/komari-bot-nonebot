"""TSK-224 slice3: 事件门控框架验收 A-E。

A 12类有效群事件空blacklist，对应family probe，五阶段bot0，telemetry total1/policy_admitted1/family0。
B 3类blacklist[100]：trace空bot0 total1/policy_restricted1/family0。
C admitted GroupMessage下run_preprocessor抛IgnoredException：rule到、handler/run_post缺席、event_post执行，集合语义。
D admitted群下两个test event preprocessor共用order：gate_start→side_effect顺序严格，phase_trace空（rule/run_pre/handler/run_post/event_post未执行）。
E 五registries快照进出context=快照，module import residue不计。

门禁未注册时：A组telemetry family0未赋值，B未拦截，C/D/E仍按预期运行。

group-business 事件类从 ``entry_gate_census.ONEBOT_EVENT_CENSUS`` 消费，确保每个 census
类获得行为 anchor 且无重复列表。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from nonebot import on_message
from nonebot.adapters.onebot.v11.event import (
    Event,
    GroupMessageEvent,
)
from nonebot.exception import IgnoredException
from nonebot.message import (
    event_postprocessor,
    event_preprocessor,
    run_postprocessor,
    run_preprocessor,
)

from tests.group_admission.entry_gate_census import ONEBOT_EVENT_CENSUS
from tests.group_admission.entry_gate_support import (
    ProbeBot,
    dispatch,
    event_gate_context,
    make_v11_event,
    read_status,
    register_phase_probe,
)
from tests.group_admission.management_support import prepare_control_plane
from tests.group_admission.observability_support import (
    FakeUtcClock,
    build_runtime_kwargs,
)
from tests.group_admission.runtime_support import AdmissionStorageFake, stored_policy

pytestmark = pytest.mark.group_admission_acceptance

# ---------------------------------------------------------------------------
# 从 census 导出 group-business 事件类参数化数据
# ---------------------------------------------------------------------------

# 类名 → 实际 class 的惰性映射
def _get_v11_class(name: str) -> type[Event]:
    """返回 V11 Event 类名对应的 class。"""
    import importlib

    mod = importlib.import_module("nonebot.adapters.onebot.v11.event")
    event_cls = mod.Event

    def _walk(cls: type[Any], seen: set[str]) -> dict[str, type[Event]]:
        result: dict[str, type[Event]] = {}
        for sub in cls.__subclasses__():
            if sub.__module__ == mod.__name__ and sub.__name__ not in seen:
                seen.add(sub.__name__)
                result[sub.__name__] = sub
                result.update(_walk(sub, seen))
        return result

    cls_map = _walk(event_cls, set())
    return cls_map[name]


def _make_overrides(class_name: str, family: str = "message") -> dict[str, object]:
    """为 census 事件类构造测试覆盖字段。

    ``post_type`` 从 ``family`` 派生，确保 notice/request 不继承 common message。
    """
    base: dict[str, object] = {"group_id": 100, "post_type": family}
    if class_name == "GroupMessageEvent":
        base["message_type"] = "group"
    elif class_name == "GroupRequestEvent":
        base["request_type"] = "group"
        base["sub_type"] = "add"
        base["flag"] = "test"
    elif class_name in (
        "GroupUploadNoticeEvent", "GroupAdminNoticeEvent",
        "GroupDecreaseNoticeEvent", "GroupIncreaseNoticeEvent",
        "GroupBanNoticeEvent", "GroupRecallNoticeEvent",
        "NotifyEvent",
    ):
        base["notice_type"] = _notice_type(class_name)
        if class_name == "GroupAdminNoticeEvent":
            base["sub_type"] = "set"
        elif class_name == "GroupDecreaseNoticeEvent":
            base["sub_type"] = "leave"
        elif class_name == "GroupIncreaseNoticeEvent":
            base["sub_type"] = "approve"
        elif class_name == "GroupBanNoticeEvent":
            base["sub_type"] = "ban"
        elif class_name == "NotifyEvent":
            base["sub_type"] = ""
    elif class_name in ("PokeNotifyEvent", "LuckyKingNotifyEvent", "HonorNotifyEvent"):
        base["notice_type"] = "notify"
        if class_name == "PokeNotifyEvent":
            base["sub_type"] = "poke"
        elif class_name == "LuckyKingNotifyEvent":
            base["sub_type"] = "lucky_king"
        elif class_name == "HonorNotifyEvent":
            base["sub_type"] = "honor"
    return base


def _notice_type(class_name: str) -> str:
    mapping = {
        "GroupUploadNoticeEvent": "group_upload",
        "GroupAdminNoticeEvent": "group_admin",
        "GroupDecreaseNoticeEvent": "group_decrease",
        "GroupIncreaseNoticeEvent": "group_increase",
        "GroupBanNoticeEvent": "group_ban",
        "GroupRecallNoticeEvent": "group_recall",
        "NotifyEvent": "notify",
    }
    return mapping.get(class_name, "")


#: 从 census 导出的 group-business 事件行（12 个）
_GROUP_BUSINESS_ROWS = [
    row for row in ONEBOT_EVENT_CENSUS if row.category == "group_business"
]

#: parametrize 用的 (cls, overrides, family) 元组列表
_GROUP_BUSINESS_PARAMS = [
    (
        _get_v11_class(row.event_class),
        _make_overrides(row.event_class, row.family),
        row.family,
    )
    for row in _GROUP_BUSINESS_ROWS
]

_GROUP_BUSINESS_IDS = [row.event_class for row in _GROUP_BUSINESS_ROWS]


#: 三个代表性 group-business 事件（B 类全测全量 12 个太慢，抽样 3 个）
_REPRESENTATIVE_PARAMS = [
    _GROUP_BUSINESS_PARAMS[0],  # GroupMessageEvent
    _GROUP_BUSINESS_PARAMS[5],  # GroupBanNoticeEvent
    _GROUP_BUSINESS_PARAMS[11],  # GroupRequestEvent
]
_REPRESENTATIVE_IDS = [
    _GROUP_BUSINESS_IDS[0],
    _GROUP_BUSINESS_IDS[5],
    _GROUP_BUSINESS_IDS[11],
]


async def _env(monkeypatch: pytest.MonkeyPatch, policy: dict[str, object]) -> Any:
    storage = AdmissionStorageFake(stored_policy(1, policy))
    app, _rt, _mgr = await prepare_control_plane(
        monkeypatch, storage, runtime_kwargs=build_runtime_kwargs(clock=FakeUtcClock()),
    )
    return app


async def _trace_telemetry_bot(app: Any, event: Event, family: str) -> tuple[list[str], dict[str, Any], ProbeBot]:
    bot = ProbeBot()
    trace: list[str] = []
    async with event_gate_context():
        register_phase_probe(trace, family)
        await dispatch(bot, event)
    body = await read_status(app)
    return trace, body["telemetry"], bot


# A ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("cls", "overrides", "family"),
    _GROUP_BUSINESS_PARAMS,
    ids=_GROUP_BUSINESS_IDS,
)
async def test_a_admitted_group_events(
    monkeypatch: pytest.MonkeyPatch, cls: type[Event], overrides: dict[str, object], family: str,
) -> None:
    """12 类有效群事件空 blacklist：trace 完整五阶段、bot 零、遥测 total1/policy_admitted1/family0。"""
    app = await _env(monkeypatch, {"mode": "blacklist", "group_ids": []})
    event = make_v11_event(cls, **overrides)
    trace, telemetry, bot = await _trace_telemetry_bot(app, event, family)
    assert trace == ["rule", "run_pre", "handler", "run_post", "event_post"], f"trace={trace}"
    assert bot.calls == [], f"bot.calls={bot.calls}"
    assert telemetry["adjudications_total"] == 1
    assert telemetry["by_reason_code"]["policy_admitted"] == 1
    assert telemetry["attribution_failures_by_event_family"][family] == 0


# B ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("cls", "overrides", "family"),
    _REPRESENTATIVE_PARAMS,
    ids=_REPRESENTATIVE_IDS,
)
async def test_b_restricted_group_events(
    monkeypatch: pytest.MonkeyPatch, cls: type[Event], overrides: dict[str, object], family: str,
) -> None:
    """3 类代表性群事件 blacklist[100]：trace 空、bot 0、total1/policy_restricted1/family0。"""
    app = await _env(monkeypatch, {"mode": "blacklist", "group_ids": [100]})
    event = make_v11_event(cls, **overrides)
    trace, telemetry, bot = await _trace_telemetry_bot(app, event, family)
    assert trace == [], f"trace={trace}"
    assert bot.calls == [], f"bot.calls={bot.calls}"
    assert telemetry["adjudications_total"] == 1
    assert telemetry["by_reason_code"]["policy_restricted"] == 1
    assert telemetry["attribution_failures_by_event_family"][family] == 0


# C ---------------------------------------------------------------------------

async def test_c_downstream_narrowing(monkeypatch: pytest.MonkeyPatch) -> None:
    _app = await _env(monkeypatch, {"mode": "blacklist", "group_ids": []})
    bot = ProbeBot()
    trace: set[str] = set()
    event = make_v11_event(GroupMessageEvent, group_id=100, message_type="group")
    async with event_gate_context():
        m = on_message(rule=lambda: (trace.add("rule"), True)[1], priority=1, block=False)
        @m.handle()
        async def _h() -> None: trace.add("handler")
        @run_preprocessor
        async def _n() -> None:
            trace.add("run_pre")
            raise IgnoredException("narrow")
        @run_postprocessor
        async def _rp() -> None: trace.add("run_post")
        @event_postprocessor
        async def _ep() -> None: trace.add("event_post")
        await dispatch(bot, event)
    assert "rule" in trace and "handler" not in trace, f"rule/handler: {trace}"
    assert "run_pre" in trace and "run_post" not in trace, f"run_pre/run_post: {trace}"
    assert "event_post" in trace, f"event_post: {trace}"
    assert bot.calls == []


# D ---------------------------------------------------------------------------

async def test_d_concurrent_preprocessors(monkeypatch: pytest.MonkeyPatch) -> None:
    _app = await _env(monkeypatch, {"mode": "blacklist", "group_ids": []})
    bot = ProbeBot()
    trace: list[str] = []
    phase_trace: list[str] = []
    gate_started = asyncio.Event()
    side_done = asyncio.Event()
    event = make_v11_event(GroupMessageEvent, group_id=100, message_type="group")
    async with event_gate_context():
        register_phase_probe(phase_trace, "message")
        ep = event_preprocessor
        @ep
        async def _sr() -> None:
            trace.append("gate_start")
            gate_started.set()
            await asyncio.wait_for(side_done.wait(), timeout=5)
            raise IgnoredException("reject")
        @ep
        async def _sh() -> None:
            await asyncio.wait_for(gate_started.wait(), timeout=5)
            trace.append("side_effect")
            side_done.set()
        await dispatch(bot, event)
    assert trace == ["gate_start", "side_effect"], f"trace={trace}"
    assert phase_trace == [], f"phase_trace={phase_trace}"
    assert bot.calls == []


# E ---------------------------------------------------------------------------

async def test_e_registry_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.group_admission.entry_gate_support import snapshot_event_registries
    _app = await _env(monkeypatch, {"mode": "blacklist", "group_ids": []})
    snapshot = snapshot_event_registries()
    bot = ProbeBot()
    trace: list[str] = []
    async with event_gate_context():
        register_phase_probe(trace, "message")
        await dispatch(bot, make_v11_event(GroupMessageEvent, group_id=100, message_type="group"))
    after = snapshot_event_registries()
    assert after["matchers"].keys() == snapshot["matchers"].keys()
    for k in snapshot["matchers"]:
        assert len(after["matchers"][k]) == len(snapshot["matchers"][k])
    for k in ("run_pre","run_post","event_post","event_pre"):
        assert after[k] == snapshot[k], f"{k} 恢复不一致"
