"""TSK-278 RED baseline: strict QQ group-@-message handler seam.

The red root for this file is the missing top-level ``RouletteQQHandler``
symbol.  Assertions follow ``TSK-278-contract.md`` section 7: strict
GroupAtMessageCreateEvent eligibility, empty-identity rejection, parse-driven
dispatch, one execute + one deliver, observation pre-read for active commands
only, and no SQL/domain/random access (collaborators are injected fakes).
"""

from __future__ import annotations

from typing import Any

from komari_bot.plugins.komari_roulette import (
    CommandReceipt,
    CommandRequest,
    Observation,
    RouletteQQHandler,
)

from .tsk278_support import (
    APP_ID,
    GROUP_OPENID,
    FakeCommandService,
    FakeDelivery,
    FakeQQBot,
    make_c2c_event,
    make_direct_event,
    make_group_at_event,
    make_plain_group_event,
    projection,
    receipt,
)


def _handler(
    *,
    service: FakeCommandService | None = None,
    delivery: FakeDelivery | None = None,
) -> tuple[RouletteQQHandler, FakeCommandService, FakeDelivery]:
    fake_service = service or FakeCommandService()
    fake_delivery = delivery or FakeDelivery(outcomes=["NO_CLAIM"])
    handler = RouletteQQHandler(service=fake_service, delivery=fake_delivery)
    return handler, fake_service, fake_delivery


def _success_receipt() -> CommandReceipt:
    return receipt(reply=projection("> 测试正文。"))


def _execute_success(service: FakeCommandService) -> None:
    service.receipt = _success_receipt()


async def _run(handler: RouletteQQHandler, bot: Any, event: Any) -> None:
    await handler.handle(bot, event)


# ---------------------------------------------------------------------------
# Strict event eligibility
# ---------------------------------------------------------------------------


async def test_ignores_plain_group_message() -> None:
    handler, service, delivery = _handler()
    await _run(handler, FakeQQBot(), make_plain_group_event("/轮盘 开枪"))
    assert service.execute_calls == []
    assert delivery.deliver_calls == []


async def test_ignores_c2c_message() -> None:
    handler, service, delivery = _handler()
    await _run(handler, FakeQQBot(), make_c2c_event("/轮盘 开枪"))
    assert service.execute_calls == []
    assert delivery.deliver_calls == []


async def test_ignores_guild_direct_message() -> None:
    handler, service, delivery = _handler()
    await _run(handler, FakeQQBot(), make_direct_event("/轮盘 开枪"))
    assert service.execute_calls == []
    assert delivery.deliver_calls == []


async def test_ignores_group_at_with_empty_message_id() -> None:
    handler, service, delivery = _handler()
    event = make_group_at_event("/轮盘 开枪", message_id="")
    await _run(handler, FakeQQBot(), event)
    assert service.execute_calls == []
    assert delivery.deliver_calls == []


async def test_ignores_group_at_with_missing_member() -> None:
    handler, service, delivery = _handler()
    event = make_group_at_event("/轮盘 开枪", member_openid="")
    await _run(handler, FakeQQBot(), event)
    assert service.execute_calls == []
    assert delivery.deliver_calls == []


# ---------------------------------------------------------------------------
# Parse-driven dispatch
# ---------------------------------------------------------------------------


async def test_ignores_non_roulette_text() -> None:
    handler, service, delivery = _handler()
    await _run(handler, FakeQQBot(), make_group_at_event("你好"))
    assert service.execute_calls == []
    assert delivery.deliver_calls == []


async def test_executes_shoot_with_observation() -> None:
    handler, service, delivery = _handler()
    _execute_success(service)
    service.observation = Observation(game_id="game-1", state_revision=7, turn_seq=3)
    await _run(handler, FakeQQBot(), make_group_at_event("/轮盘 开枪"))

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
    await _run(handler, FakeQQBot(), make_group_at_event("/轮盘 道具 使用 D0"))

    assert len(service.execute_calls) == 1
    request, _observation = service.execute_calls[0]
    assert request.command.intent == "syntax_failure"
    assert request.command.syntax_code == "invalid_player_seq"
    assert len(delivery.deliver_calls) == 1


async def test_waiting_command_does_not_pre_read_observation() -> None:
    handler, service, delivery = _handler()
    _execute_success(service)
    await _run(handler, FakeQQBot(), make_group_at_event("/轮盘 开局"))

    assert service.observe_calls == []
    assert len(service.execute_calls) == 1
    _request, observation = service.execute_calls[0]
    assert observation is None
    assert len(delivery.deliver_calls) == 1


async def test_open_item_panel_does_not_pre_read_observation() -> None:
    handler, service, delivery = _handler()
    _execute_success(service)
    await _run(handler, FakeQQBot(), make_group_at_event("/轮盘 道具"))

    assert service.observe_calls == []
    assert len(service.execute_calls) == 1
    request, observation = service.execute_calls[0]
    assert request.command.intent == "open_item_panel"
    assert observation is None
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
    )

    request, _observation = service.execute_calls[0]
    assert request.app_id == "tsk278-app"
    assert request.group_openid == GROUP_OPENID
    assert request.inbound_msg_id == "qq-msg-42"
    assert request.member_openid == "member-7"


async def test_request_mention_count_matches_event_mentions() -> None:
    handler, service, _delivery = _handler()
    _execute_success(service)
    mentions = [{"id": "x", "type": "mention_user"}]
    await _run(
        handler,
        FakeQQBot(),
        make_group_at_event("/轮盘 开枪", mentions=mentions),
    )
    request, _observation = service.execute_calls[0]
    assert request.target_mention_count == 1


# ---------------------------------------------------------------------------
# Boundary: handler performs no SQL / domain writes / randomness
# ---------------------------------------------------------------------------


async def test_handler_only_touches_injected_service_and_delivery() -> None:
    handler, service, delivery = _handler()
    _execute_success(service)
    await _run(handler, FakeQQBot(), make_group_at_event("/轮盘 开枪"))

    # 编排只触碰注入的 service/delivery：一次 observe、一次 execute、一次 deliver。
    assert len(service.observe_calls) == 1
    assert len(service.execute_calls) == 1
    assert len(delivery.deliver_calls) == 1
    # 履约相关调用绝不由 handler 直接触发。
    assert service.claim_calls == []
    assert service.mark_delivered_calls == []
    assert service.mark_not_delivered_calls == []
