"""TSK-278 RED baseline: one-shot QQ delivery seam.

The red root for this file is the missing top-level ``RouletteDelivery`` /
``DeliveryOutcome`` / ``SendNotAcceptedError`` symbols.  Assertions follow
``TSK-278-contract.md`` section 6 and TSK-267: frozen payload, msg_id=inbound,
msg_seq=1, no message_reference, at most one send, no retry after unknown
outcomes, no network call on claim failure / duplicate / pre-send failure.
"""

from __future__ import annotations

import asyncio

import pytest

from komari_bot.plugins.komari_roulette import (
    CommandReceipt,
    DeliveryOutcome,
    RouletteDelivery,
    SendNotAcceptedError,
    StorageUnavailableError,
)

from .tsk278_support import (
    FakeCommandService,
    FakeSender,
    claim,
    projection,
    receipt,
)


def _delivery(*, service: FakeCommandService | None = None) -> RouletteDelivery:
    return RouletteDelivery(service=service or FakeCommandService())


def _success_receipt() -> CommandReceipt:
    return receipt(
        reply=projection(
            "> 小明又逃过一劫，获得了啤酒。\n\n**当前：小明**",
            mention_member_openid="member-1",
            mention_display_name="小明",
        )
    )


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


async def test_deliver_success_marks_delivered() -> None:
    service = FakeCommandService()
    service.claim_result = claim("receipt-1")
    sender = FakeSender(result="qq-platform-msg-9")
    outcome = await _delivery(service=service).deliver(_success_receipt(), sender)

    assert outcome is DeliveryOutcome.DELIVERED
    assert len(sender.calls) == 1
    assert len(sender.network_calls) == 1
    assert service.mark_delivered_calls == [
        (claim("receipt-1"), "qq-platform-msg-9")
    ]
    assert service.mark_not_delivered_calls == []


async def test_deliver_uses_inbound_msg_id_and_seq_1_without_reference() -> None:
    service = FakeCommandService()
    service.claim_result = claim("receipt-1")
    sender = FakeSender()
    await _delivery(service=service).deliver(_success_receipt(), sender)

    payload = sender.calls[0]
    assert payload["group_openid"] == "group-1"
    assert payload["msg_id"] == "msg-1"
    assert payload["msg_seq"] == 1
    assert "message_reference" not in payload
    assert "msg_ref_id" not in payload


async def test_deliver_builds_payload_from_frozen_receipt() -> None:
    service = FakeCommandService()
    service.claim_result = claim("receipt-1")
    sender = FakeSender()
    frozen = _success_receipt()
    await _delivery(service=service).deliver(frozen, sender)

    message = sender.calls[0]["message"]
    text = str(getattr(message, "content", message))
    assert frozen.reply.body in text
    # 送达只读冻结收据：不 observe、不重读当前状态。
    assert service.observe_calls == []
    assert service.execute_calls == []


# ---------------------------------------------------------------------------
# Pre-send failures: explicit, no network call
# ---------------------------------------------------------------------------


async def test_deliver_explicit_not_accepted_marks_not_delivered() -> None:
    service = FakeCommandService()
    service.claim_result = claim("receipt-1")
    sender = FakeSender(
        mode="fail_before_send",
        exc=SendNotAcceptedError("credentials expired"),
    )
    outcome = await _delivery(service=service).deliver(_success_receipt(), sender)

    assert outcome is DeliveryOutcome.NOT_DELIVERED
    assert len(sender.calls) == 1  # 一次 send 尝试
    assert sender.network_calls == []  # 但明确未发生网络调用
    assert service.mark_not_delivered_calls == [claim("receipt-1")]
    assert service.mark_delivered_calls == []


# ---------------------------------------------------------------------------
# Post-send unknown outcomes: keep PENDING_CONFIRMATION, never retry
# ---------------------------------------------------------------------------


async def test_deliver_timeout_is_unknown_no_retry() -> None:
    service = FakeCommandService()
    service.claim_result = claim("receipt-1")
    sender = FakeSender(mode="fail_after_send", exc=TimeoutError("boom"))
    outcome = await _delivery(service=service).deliver(_success_receipt(), sender)

    assert outcome is DeliveryOutcome.UNKNOWN
    assert len(sender.network_calls) == 1
    assert len(sender.calls) == 1
    assert service.mark_delivered_calls == []
    assert service.mark_not_delivered_calls == []


async def test_deliver_crash_is_unknown_no_retry() -> None:
    service = FakeCommandService()
    service.claim_result = claim("receipt-1")
    sender = FakeSender(mode="fail_after_send", exc=ConnectionError("gone"))
    outcome = await _delivery(service=service).deliver(_success_receipt(), sender)

    assert outcome is DeliveryOutcome.UNKNOWN
    assert len(sender.network_calls) == 1
    assert len(sender.calls) == 1
    assert service.mark_delivered_calls == []
    assert service.mark_not_delivered_calls == []


async def test_deliver_cancel_is_not_swallowed_and_not_retried() -> None:
    service = FakeCommandService()
    service.claim_result = claim("receipt-1")
    sender = FakeSender(mode="cancel")
    with pytest.raises(asyncio.CancelledError):
        await _delivery(service=service).deliver(_success_receipt(), sender)

    assert len(sender.network_calls) == 1
    assert len(sender.calls) == 1
    assert service.mark_delivered_calls == []
    assert service.mark_not_delivered_calls == []


async def test_deliver_mark_failure_after_send_is_unknown_no_retry() -> None:
    service = FakeCommandService()
    service.claim_result = claim("receipt-1")
    service.mark_delivered_error = StorageUnavailableError("db down")
    sender = FakeSender(result="qq-platform-msg-9")
    outcome = await _delivery(service=service).deliver(_success_receipt(), sender)

    assert outcome is DeliveryOutcome.UNKNOWN
    assert len(sender.network_calls) == 1
    assert len(sender.calls) == 1
    assert service.mark_delivered_calls == [
        (claim("receipt-1"), "qq-platform-msg-9")
    ]


# ---------------------------------------------------------------------------
# Claim failures: no network call, propagate storage errors
# ---------------------------------------------------------------------------


async def test_deliver_claim_failure_no_network_call() -> None:
    service = FakeCommandService()
    service.claim_error = StorageUnavailableError("claim storage down")
    sender = FakeSender()
    with pytest.raises(StorageUnavailableError):
        await _delivery(service=service).deliver(_success_receipt(), sender)

    assert sender.calls == []
    assert sender.network_calls == []
    assert service.mark_delivered_calls == []
    assert service.mark_not_delivered_calls == []


# ---------------------------------------------------------------------------
# Duplicate event: at most one network call
# ---------------------------------------------------------------------------


async def test_duplicate_event_is_single_network_call() -> None:
    service = FakeCommandService()
    service.claim_result = claim("receipt-1")
    sender = FakeSender(result="qq-platform-msg-9")
    delivery = _delivery(service=service)

    first = await delivery.deliver(_success_receipt(), sender)
    assert first is DeliveryOutcome.DELIVERED

    # 重复事件/已领取：claim 返回 None，第二次不再调用发送。
    service.claim_result = None
    second = await delivery.deliver(_success_receipt(), sender)
    assert second is DeliveryOutcome.NO_CLAIM

    assert len(sender.calls) == 1
    assert len(sender.network_calls) == 1
    assert service.claim_calls == ["receipt-1", "receipt-1"]


async def test_duplicate_platform_id_is_idempotent() -> None:
    service = FakeCommandService()
    service.claim_result = claim("receipt-1")
    sender = FakeSender(result="qq-platform-msg-9")
    delivery = _delivery(service=service)

    await delivery.deliver(_success_receipt(), sender)
    service.claim_result = None
    await delivery.deliver(_success_receipt(), sender)

    assert service.mark_delivered_calls == [
        (claim("receipt-1"), "qq-platform-msg-9")
    ]


async def test_deliver_never_reads_game_state() -> None:
    service = FakeCommandService()
    service.claim_result = claim("receipt-1")
    sender = FakeSender()
    await _delivery(service=service).deliver(_success_receipt(), sender)
    # 履约编排只触碰 claim/mark 与 sender；绝不 observe/execute。
    assert service.observe_calls == []
    assert service.execute_calls == []
    assert service.claim_calls == ["receipt-1"]
