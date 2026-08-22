"""TSK-224 事件门控流验收：前置处理器注册与消息流阶段序。

验收目标（TSK-224 本 slice 冻结）：

- 生产 ``event_gate`` 模块通过 ``group_admission.__init__`` 底层 import
  自动注册一个 ``event_preprocessor`` 前置处理器（不提供 ``install_event_gate``
  测试接缝）；
- 已鉴权准入策略空黑名单（全部群获准）时，消息经完整五阶段流：
  ``[rule, run_pre, handler, run_post, event_post]``；
- 受限群（黑名单[100]）时，消息被门控前置处理器拦截，经过零阶段、无
  Bot 调用、状态遥测 ``policy_restricted=1``；
- 全部注册表快照与恢复由 ``event_gate_context`` 管理，无永久泄漏。

生产 ``event_gate`` 模块未注册其 ``event_preprocessor`` 时，消息
流经完整五阶段而非零阶段，测试失败（RED）。

``register_message_phase_probe`` 已移除，改用 ``register_phase_probe(trace, 'message')``。
"""

from __future__ import annotations

import pytest
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
from nonebot.adapters.onebot.v11.event import Sender

from tests.group_admission.entry_gate_support import (
    ProbeBot,
    dispatch,
    event_gate_context,
    register_phase_probe,
)
from tests.group_admission.management_support import (
    READER_TOKEN,
    STATUS_PATH,
    asgi_client,
    auth_headers,
    prepare_control_plane,
)
from tests.group_admission.runtime_support import (
    AdmissionStorageFake,
    stored_policy,
)

pytestmark = pytest.mark.group_admission_acceptance


def _build_group_event(group_id: int) -> GroupMessageEvent:
    """构造一个合法的 OneBot V11 群消息事件。"""
    message = Message("hello")
    sender = Sender.model_construct(
        user_id=67890, nickname="tester", card=""
    )
    return GroupMessageEvent.model_construct(
        time=1000000,
        self_id=12345,
        post_type="message",
        sub_type="normal",
        user_id=67890,
        group_id=group_id,
        message=message,
        original_message=message,
        raw_message="hello",
        sender=sender,
        message_type="group",
        message_id=1,
        font=0,
    )


async def test_event_gate_admitted_message_observes_all_phases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空黑名单（全部群获准）：消息流经完整五阶段。

    生产 ``event_gate`` 模块未注册其 ``event_preprocessor`` 时，
    ``event_preprocessor`` 不拦截消息，消息流经完整五阶段而非零阶段，
    测试失败（RED）。
    """
    storage = AdmissionStorageFake(
        stored_policy(1, {"mode": "blacklist", "group_ids": []})
    )
    _app, _runtime, _manager = await prepare_control_plane(monkeypatch, storage)

    bot = ProbeBot()
    trace: list[str] = []

    async with event_gate_context():
        register_phase_probe(trace, "message")
        event = _build_group_event(group_id=1)  # 空黑名单，全部获准
        await dispatch(bot, event)

    # 消息应经完整五阶段
    assert trace == [
        "rule",
        "run_pre",
        "handler",
        "run_post",
        "event_post",
    ], f"trace={trace}"
    # 门控不应产生任何 Bot API 调用
    assert bot.calls == [], f"bot.calls={bot.calls}"


async def test_event_gate_restricted_message_observes_no_phases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """黑名单[100]：消息被门控拦截，零阶段、零 Bot 调用，遥测 policy_restricted=1。

    生产 ``event_gate`` 模块未注册其 ``event_preprocessor`` 时，
    ``event_preprocessor`` 不拦截消息，消息流经完整五阶段而非零阶段，
    测试失败（RED）。
    """
    storage = AdmissionStorageFake(
        stored_policy(1, {"mode": "blacklist", "group_ids": [100]})
    )
    _app, _runtime, _manager = await prepare_control_plane(monkeypatch, storage)

    bot = ProbeBot()
    trace: list[str] = []

    async with event_gate_context():
        register_phase_probe(trace, "message")
        event = _build_group_event(group_id=100)  # 黑名单中，应被拦截
        await dispatch(bot, event)

    # 门控拦截：零阶段、零 Bot 调用
    assert trace == [], f"trace={trace}"
    assert bot.calls == [], f"bot.calls={bot.calls}"

    # 遥测：原有 _app（prepare 返回）查询状态
    async with asgi_client(_app) as client:
        response = await client.get(
            STATUS_PATH, headers=auth_headers(READER_TOKEN)
        )
        assert response.status_code == 200, response.text
        body = response.json()
        telemetry = body["telemetry"]
        assert telemetry["adjudications_total"] == 1, telemetry
        assert (
            telemetry["by_reason_code"]["policy_restricted"] == 1
        ), telemetry
        assert (
            telemetry["attribution_failures_by_event_family"]["message"] == 0
        ), telemetry
