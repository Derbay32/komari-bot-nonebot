"""TSK-278 RED baseline: strict QQ group-@-message handler seam.

The red roots for this file are the missing/incorrect ``RouletteQQHandler``
seam in ``komari_bot.plugins.komari_roulette.qq.handler``.  Assertions follow
``TSK-278-contract.md`` section 7: strict GroupAtMessageCreateEvent
eligibility, real admission handoff (``get_qq_admission_token``, not faked),
**token scope + identity binding** (a binding token or a token for another
app/group/member/message never authorizes a business command), a mandatory
business re-authorization gate placed *after* observe and *immediately before*
execute (revocation between observe and execute means zero writes), parse-driven
dispatch, one execute + one deliver, observation pre-read for active commands
only, send gate (plugin switch / group admission recheck) re-checked separately
before starting any send, ``target_mention_count`` read from the real parsed
``mention_user`` segments (never ``event.mentions``), and no SQL/domain/random
access (collaborators are injected).
"""

from __future__ import annotations

from typing import Any

import pytest

from komari_bot.plugins.komari_roulette import (
    CommandReceipt,
    CommandRequest,
    Observation,
)
from komari_bot.plugins.komari_roulette.qq.handler import RouletteQQHandler

from .tsk278_support import (
    APP_ID,
    GROUP_OPENID,
    FakeCommandService,
    FakeDelivery,
    FakeQQBot,
    admission_state,
    business_token,
    event_mention_count,
    live_get_qq_admission_token,
    make_c2c_event,
    make_direct_event,
    make_group_at_event,
    make_plain_group_event,
    make_real_group_at_event,
    projection,
    receipt,
)

_MISSING = object()


def _handler(
    *,
    service: FakeCommandService | None = None,
    delivery: FakeDelivery | None = None,
    send_gate: Any = None,
    business_gate: Any = _MISSING,
) -> tuple[RouletteQQHandler, FakeCommandService, FakeDelivery]:
    fake_service = service or FakeCommandService()
    fake_delivery = delivery or FakeDelivery()
    if business_gate is _MISSING:
        business_gate = _allow_gate
    handler = RouletteQQHandler(
        service=fake_service,
        delivery=fake_delivery,
        business_gate=business_gate,  # type: ignore[call-arg]  # RED: 生产尚未接受该必填参数
        send_gate=send_gate,
    )
    return handler, fake_service, fake_delivery


def _allow_gate(_bot: Any, _event: Any, _token: Any) -> bool:
    return True


def _success_receipt() -> CommandReceipt:
    return receipt(reply=projection("> 测试正文。"))


def _execute_success(service: FakeCommandService) -> None:
    service.receipt = _success_receipt()


async def _run(
    handler: RouletteQQHandler,
    bot: Any,
    event: Any,
    *,
    state: dict[str, Any] | None = None,
) -> None:
    await handler.handle(bot, event, state=state)


# ---------------------------------------------------------------------------
# Strict event eligibility
# ---------------------------------------------------------------------------


async def test_ignores_plain_group_message() -> None:
    handler, service, delivery = _handler()
    await _run(
        handler,
        FakeQQBot(),
        make_plain_group_event("/轮盘 开枪"),
        state=admission_state(),
    )
    assert service.execute_calls == []
    assert delivery.deliver_calls == []


async def test_ignores_c2c_message() -> None:
    handler, service, delivery = _handler()
    await _run(
        handler, FakeQQBot(), make_c2c_event("/轮盘 开枪"), state=admission_state()
    )
    assert service.execute_calls == []
    assert delivery.deliver_calls == []


async def test_ignores_guild_direct_message() -> None:
    handler, service, delivery = _handler()
    await _run(
        handler,
        FakeQQBot(),
        make_direct_event("/轮盘 开枪"),
        state=admission_state(),
    )
    assert service.execute_calls == []
    assert delivery.deliver_calls == []


async def test_ignores_group_at_with_empty_message_id() -> None:
    handler, service, delivery = _handler()
    event = make_group_at_event("/轮盘 开枪", message_id="")
    await _run(handler, FakeQQBot(), event, state=admission_state())
    assert service.execute_calls == []
    assert delivery.deliver_calls == []


async def test_ignores_group_at_with_missing_member() -> None:
    handler, service, delivery = _handler()
    event = make_group_at_event("/轮盘 开枪", member_openid="")
    await _run(handler, FakeQQBot(), event, state=admission_state())
    assert service.execute_calls == []
    assert delivery.deliver_calls == []


# ---------------------------------------------------------------------------
# Real admission handoff (TSK-274 gate token; helper is real, not faked)
# ---------------------------------------------------------------------------


async def test_no_state_is_silent() -> None:
    handler, service, delivery = _handler()
    await _run(handler, FakeQQBot(), make_group_at_event("/轮盘 开枪"))
    assert service.execute_calls == []
    assert delivery.deliver_calls == []


async def test_empty_state_is_silent() -> None:
    handler, service, delivery = _handler()
    await _run(
        handler, FakeQQBot(), make_group_at_event("/轮盘 开枪"), state={}
    )
    assert service.execute_calls == []
    assert delivery.deliver_calls == []


async def test_admission_token_reads_real_state_key() -> None:
    # handler 必须用真实 get_qq_admission_token 读取 NoneBot state 中的准入 token。
    state = admission_state(token=business_token(member_openid="member-1"))
    token = live_get_qq_admission_token(state)
    assert token is not None and token.member_openid == "member-1"

    handler, service, delivery = _handler()
    _execute_success(service)
    await _run(
        handler,
        FakeQQBot(),
        make_group_at_event("/轮盘 开枪", member_openid="member-1"),
        state=state,
    )
    assert len(service.execute_calls) == 1
    assert len(delivery.deliver_calls) == 1


async def test_authoritative_none_never_falls_back_to_import_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """权威 ``get_qq_admission_token`` 明确返回 None → 静默拒绝，绝不回退旧 helper。

    import-time 绑定的 helper 是一个不同的模块代类；只要权威 helper 说
    "没有 token"，它就不能被用来补一个 token 继续执行。 同时锁定权威 helper
    每次事件只调用一次。
    """

    from komari_bot.plugins import group_admission
    from komari_bot.plugins.komari_roulette.qq import handler as handler_module

    state = admission_state()
    live_calls: list[object] = []
    legacy_calls: list[object] = []

    def authoritative_helper(received: object) -> None:
        live_calls.append(received)

    def stale_import_helper(received: object) -> Any:
        legacy_calls.append(received)
        return business_token()

    monkeypatch.setattr(
        group_admission, "get_qq_admission_token", authoritative_helper
    )
    monkeypatch.setattr(
        handler_module,
        "_get_qq_admission_token",
        stale_import_helper,
        raising=False,
    )

    handler, service, delivery = _handler()
    _execute_success(service)
    await _run(
        handler,
        FakeQQBot(),
        make_group_at_event("/轮盘 开枪"),
        state=state,
    )

    assert live_calls == [state], "权威 helper 必须恰好调用一次"
    assert legacy_calls == [], "权威返回 None 后不得回退旧 import-time helper"
    assert service.observe_calls == []
    assert service.execute_calls == []
    assert delivery.deliver_calls == []


# ---------------------------------------------------------------------------
# Token scope + identity binding (presence alone must never authorize)
# ---------------------------------------------------------------------------


async def test_binding_scope_token_never_authorizes_business_command() -> None:
    """绑定/绑定挑战 token 不得当作业务令牌使用。"""
    handler, service, delivery = _handler()
    _execute_success(service)
    await _run(
        handler,
        FakeQQBot(),
        make_group_at_event("/轮盘 开枪"),
        state=admission_state(token=business_token(scope="binding")),
    )
    assert service.observe_calls == []
    assert service.execute_calls == []
    assert delivery.deliver_calls == []


async def test_binding_challenge_scope_token_never_authorizes_business_command() -> None:
    handler, service, delivery = _handler()
    _execute_success(service)
    await _run(
        handler,
        FakeQQBot(),
        make_group_at_event("/轮盘 开枪"),
        state=admission_state(token=business_token(scope="binding_challenge")),
    )
    assert service.observe_calls == []
    assert service.execute_calls == []
    assert delivery.deliver_calls == []


@pytest.mark.parametrize(
    "token_kwargs",
    [
        pytest.param({"app_id": "other-app"}, id="wrong-app"),
        pytest.param({"group_openid": "other-group"}, id="wrong-group"),
        pytest.param({"member_openid": "member-9"}, id="wrong-member"),
        pytest.param({"qq_message_id": "msg-9"}, id="wrong-message"),
    ],
)
async def test_identity_mismatch_token_never_authorizes(
    token_kwargs: dict[str, Any],
) -> None:
    """token 与事件必须同源：app/group/member/message 任一不符即静默拒绝。"""
    handler, service, delivery = _handler()
    _execute_success(service)
    await _run(
        handler,
        FakeQQBot(),
        make_group_at_event("/轮盘 开枪"),
        state=admission_state(token=business_token(**token_kwargs)),
    )
    assert service.observe_calls == []
    assert service.execute_calls == []
    assert delivery.deliver_calls == []


# ---------------------------------------------------------------------------
# Business re-authorization gate (mandatory, after observe, before execute)
# ---------------------------------------------------------------------------


def test_business_gate_is_mandatory() -> None:
    """没有业务重授权门即构造失败——本票不提供 presence-only 宽松默认值。"""
    service = FakeCommandService()
    delivery = FakeDelivery()
    with pytest.raises(TypeError):
        RouletteQQHandler(service=service, delivery=delivery)  # type: ignore[call-arg]


async def test_business_gate_runs_after_observe_and_before_execute() -> None:
    seen: list[tuple[str, int, int]] = []
    service = FakeCommandService()
    delivery = FakeDelivery()
    _execute_success(service)
    service.observation = Observation(game_id="game-1", state_revision=7, turn_seq=3)

    def recording_gate(_bot: Any, _event: Any, _token: Any) -> bool:
        seen.append(("gate", len(service.observe_calls), len(service.execute_calls)))
        return True

    handler, service, delivery = _handler(
        service=service, delivery=delivery, business_gate=recording_gate
    )

    await _run(
        handler,
        FakeQQBot(),
        make_group_at_event("/轮盘 开枪"),
        state=admission_state(),
    )

    assert seen == [("gate", 1, 0)], (
        "business_gate 必须在 observe_current 之后、execute_group_command 之前调用"
    )
    assert len(service.execute_calls) == 1
    assert len(delivery.deliver_calls) == 1


async def test_business_gate_receives_bot_event_and_token() -> None:
    received: list[tuple[Any, Any, Any]] = []

    def gate(bot: Any, event: Any, token: Any) -> bool:
        received.append((bot, event, token))
        return True

    handler, service, _delivery = _handler(business_gate=gate)
    _execute_success(service)
    event = make_group_at_event("/轮盘 开枪")
    token = business_token()
    await _run(handler, FakeQQBot(), event, state=admission_state(token=token))

    assert len(received) == 1
    passed_bot, passed_event, passed_token = received[0]
    assert passed_bot.self_id == APP_ID
    assert passed_event is event
    assert passed_token is token


async def test_business_gate_async_is_awaited() -> None:
    calls: list[str] = []

    async def async_gate(_bot: Any, _event: Any, _token: Any) -> bool:
        calls.append("gate")
        return True

    handler, service, delivery = _handler(business_gate=async_gate)
    _execute_success(service)
    await _run(
        handler,
        FakeQQBot(),
        make_group_at_event("/轮盘 开枪"),
        state=admission_state(),
    )
    assert calls == ["gate"]
    assert len(service.execute_calls) == 1
    assert len(delivery.deliver_calls) == 1


async def test_business_gate_false_blocks_execute_and_deliver() -> None:
    """观察后、执行前准入被撤回 → 0 领域写入、0 发送。"""
    handler, service, delivery = _handler(business_gate=lambda *_a: False)
    _execute_success(service)
    await _run(
        handler,
        FakeQQBot(),
        make_group_at_event("/轮盘 开枪"),
        state=admission_state(),
    )
    # observe 可以在 gate 之前发生（只读），但绝不 execute / deliver。
    assert service.execute_calls == []
    assert delivery.deliver_calls == []


async def test_business_gate_revocation_between_observe_and_execute_blocks_write() -> None:
    """模拟群准入/插件开关在校验后、执行前被撤回：gate 重新裁决必须拦住写入。"""

    def revoked_gate(_bot: Any, _event: Any, _token: Any) -> bool:
        return False

    handler, service, delivery = _handler(business_gate=revoked_gate)
    _execute_success(service)
    await _run(
        handler,
        FakeQQBot(),
        make_group_at_event("/轮盘 开枪"),
        state=admission_state(),
    )
    assert service.execute_calls == []
    assert delivery.deliver_calls == []


async def test_business_gate_exception_fails_closed() -> None:
    """gate 自身异常（准入查询失败）→ 故障关闭，0 execute/0 deliver。"""

    def broken_gate(_bot: Any, _event: Any, _token: Any) -> bool:
        raise RuntimeError

    handler, service, delivery = _handler(business_gate=broken_gate)
    _execute_success(service)
    await _run(
        handler,
        FakeQQBot(),
        make_group_at_event("/轮盘 开枪"),
        state=admission_state(),
    )
    assert service.execute_calls == []
    assert delivery.deliver_calls == []


# ---------------------------------------------------------------------------
# Parse-driven dispatch
# ---------------------------------------------------------------------------


async def test_ignores_non_roulette_text() -> None:
    handler, service, delivery = _handler()
    await _run(
        handler, FakeQQBot(), make_group_at_event("你好"), state=admission_state()
    )
    assert service.execute_calls == []
    assert delivery.deliver_calls == []


async def test_executes_shoot_with_observation() -> None:
    handler, service, delivery = _handler()
    _execute_success(service)
    service.observation = Observation(game_id="game-1", state_revision=7, turn_seq=3)
    await _run(
        handler,
        FakeQQBot(),
        make_group_at_event("/轮盘 开枪"),
        state=admission_state(),
    )

    assert len(service.observe_calls) == 1
    assert service.observe_calls[0].group_openid == GROUP_OPENID
    assert len(service.execute_calls) == 1
    request, observation = service.execute_calls[0]
    assert isinstance(request, CommandRequest)
    assert request.command.intent == "shoot"
    assert observation == service.observation
    assert len(delivery.deliver_calls) == 1
    assert delivery.deliver_calls[0][0] is service.receipt
    assert delivery.deliver_calls[0][1].self_id == APP_ID


async def test_executes_syntax_failure_through_same_path() -> None:
    handler, service, delivery = _handler()
    service.receipt = receipt(result_code="invalid_player_seq")
    await _run(
        handler,
        FakeQQBot(),
        make_group_at_event("/轮盘 道具 使用 D0"),
        state=admission_state(),
    )

    assert len(service.execute_calls) == 1
    request, _observation = service.execute_calls[0]
    assert request.command.intent == "syntax_failure"
    assert request.command.syntax_code == "invalid_player_seq"
    assert len(delivery.deliver_calls) == 1


async def test_waiting_command_does_not_pre_read_observation() -> None:
    handler, service, delivery = _handler()
    _execute_success(service)
    await _run(
        handler,
        FakeQQBot(),
        make_group_at_event("/轮盘 开局"),
        state=admission_state(),
    )

    assert service.observe_calls == []
    assert len(service.execute_calls) == 1
    _request, observation = service.execute_calls[0]
    assert observation is None
    assert len(delivery.deliver_calls) == 1


async def test_open_item_panel_does_not_pre_read_observation() -> None:
    handler, service, delivery = _handler()
    _execute_success(service)
    await _run(
        handler,
        FakeQQBot(),
        make_group_at_event("/轮盘 道具"),
        state=admission_state(),
    )

    assert service.observe_calls == []
    assert len(service.execute_calls) == 1
    request, observation = service.execute_calls[0]
    assert request.command.intent == "open_item_panel"
    assert observation is None
    assert len(delivery.deliver_calls) == 1


# ---------------------------------------------------------------------------
# Send gate: 轮盘开关/群准入实时核查，关闭或受限不启动新发送
# ---------------------------------------------------------------------------


async def test_send_gate_false_does_not_start_delivery() -> None:
    handler, service, delivery = _handler(send_gate=lambda: False)
    _execute_success(service)
    await _run(
        handler,
        FakeQQBot(),
        make_group_at_event("/轮盘 开枪"),
        state=admission_state(),
    )

    # 领域仍执行并冻结收据，但发送门为 False → 不启动新发送、0 网络、不重渲染。
    assert len(service.execute_calls) == 1
    assert delivery.deliver_calls == []


async def test_send_gate_runs_before_delivery_and_gates_it() -> None:
    calls: list[str] = []
    gated = {"value": False}

    def send_gate() -> bool:
        calls.append("gate")
        return gated["value"]

    handler, service, delivery = _handler(send_gate=send_gate)
    _execute_success(service)
    await _run(
        handler,
        FakeQQBot(),
        make_group_at_event("/轮盘 开枪"),
        state=admission_state(),
    )
    assert calls == ["gate"]
    assert delivery.deliver_calls == []

    # 轮盘开关恢复后同一事件再走一遍 → 恰好一次 deliver。
    gated["value"] = True
    await _run(
        handler,
        FakeQQBot(),
        make_group_at_event("/轮盘 开枪"),
        state=admission_state(),
    )
    assert len(delivery.deliver_calls) == 1


async def test_send_gate_is_independent_of_business_gate() -> None:
    """业务重授权通过后，发送时刻仍要独立重核（门顺序不可合并）。"""
    order: list[str] = []

    def business_gate(_bot: Any, _event: Any, _token: Any) -> bool:
        order.append("business")
        return True

    def send_gate() -> bool:
        order.append("send")
        return False

    handler, service, delivery = _handler(
        business_gate=business_gate, send_gate=send_gate
    )
    _execute_success(service)
    await _run(
        handler,
        FakeQQBot(),
        make_group_at_event("/轮盘 开枪"),
        state=admission_state(),
    )
    assert order == ["business", "send"]
    assert len(service.execute_calls) == 1
    assert delivery.deliver_calls == []


async def test_async_send_gate_is_awaited() -> None:
    """真实准入实时门可为异步可调用：await 结果再决定是否启动发送。"""
    calls: list[str] = []

    async def async_send_gate() -> bool:
        calls.append("gate")
        return False

    handler, service, delivery = _handler(send_gate=async_send_gate)
    _execute_success(service)
    await _run(
        handler,
        FakeQQBot(),
        make_group_at_event("/轮盘 开枪"),
        state=admission_state(),
    )

    assert calls == ["gate"]
    assert len(service.execute_calls) == 1
    assert delivery.deliver_calls == []


async def test_default_send_gate_allows_delivery() -> None:
    handler, service, delivery = _handler()
    _execute_success(service)
    await _run(
        handler,
        FakeQQBot(),
        make_group_at_event("/轮盘 开枪"),
        state=admission_state(),
    )
    assert len(delivery.deliver_calls) == 1


# ---------------------------------------------------------------------------
# Request fingerprint construction
# ---------------------------------------------------------------------------


async def test_request_fingerprint_uses_event_identity() -> None:
    handler, service, _delivery = _handler()
    _execute_success(service)
    await _run(
        handler,
        FakeQQBot(self_id="tsk278-app"),
        make_group_at_event(
            "/轮盘 开枪",
            message_id="qq-msg-42",
            member_openid="member-7",
            author_name="阿七",
        ),
        state=admission_state(
            token=business_token(
                member_openid="member-7", qq_message_id="qq-msg-42"
            )
        ),
    )

    request, _observation = service.execute_calls[0]
    assert request.app_id == "tsk278-app"
    assert request.group_openid == GROUP_OPENID
    assert request.inbound_msg_id == "qq-msg-42"
    assert request.member_openid == "member-7"


async def test_request_mention_count_comes_from_real_parsed_segments() -> None:
    """真实模型没有 `mentions` 字段；@ 必须来自解析出的 mention_user 段。"""
    handler, service, _delivery = _handler()
    _execute_success(service)
    event = make_real_group_at_event("/轮盘 开枪 <@!12345>")
    # 真实 QQ 事件没有 mentions 数组；presence-only 读 event.mentions 只会得 None。
    assert event.mentions is None
    await _run(handler, FakeQQBot(), event, state=admission_state())

    request, _observation = service.execute_calls[0]
    assert event_mention_count(event) == 1
    assert request.target_mention_count == 1


async def test_request_mention_count_is_zero_without_mention_segments() -> None:
    handler, service, _delivery = _handler()
    _execute_success(service)
    event = make_real_group_at_event("/轮盘 开枪")
    await _run(handler, FakeQQBot(), event, state=admission_state())

    request, _observation = service.execute_calls[0]
    assert event_mention_count(event) == 0
    assert request.target_mention_count == 0


async def test_request_mention_count_counts_everyone_segment() -> None:
    handler, service, _delivery = _handler()
    _execute_success(service)
    event = make_real_group_at_event("<@!all> /轮盘 开枪")
    assert event.mentions is None
    await _run(handler, FakeQQBot(), event, state=admission_state())

    request, _observation = service.execute_calls[0]
    assert event_mention_count(event) == 1
    assert request.target_mention_count == 1


# ---------------------------------------------------------------------------
# Boundary: handler performs no SQL / domain writes / randomness
# ---------------------------------------------------------------------------


async def test_handler_only_touches_injected_service_and_delivery() -> None:
    handler, service, delivery = _handler()
    _execute_success(service)
    await _run(
        handler,
        FakeQQBot(),
        make_group_at_event("/轮盘 开枪"),
        state=admission_state(),
    )

    # 编排只触碰注入的 service/delivery：一次 observe、一次 execute、一次 deliver。
    assert len(service.observe_calls) == 1
    assert len(service.execute_calls) == 1
    assert len(delivery.deliver_calls) == 1
    # 履约相关调用绝不由 handler 直接触发。
    assert service.claim_calls == []
    assert service.mark_delivered_calls == []
    assert service.mark_not_delivered_calls == []
