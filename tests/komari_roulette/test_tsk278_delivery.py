"""TSK-278 RED baseline: one-shot QQ delivery seam.

The red roots for this file are the missing ``RouletteDelivery`` /
``DeliveryOutcome`` / ``SendNotAcceptedError`` symbols in
``komari_bot.plugins.komari_roulette.qq.delivery``.  Assertions follow
``TSK-278-contract.md`` section 6 and TSK-267: frozen payload built from the
committed receipt *before* any claim, then claim → final runtime recheck →
send → mark; msg_id=inbound, msg_seq=1, no message_reference; at most one
send; no retry after unknown outcomes; no network call on claim failure /
duplicate / pre-send failure (build failure, 5-min credential expiry, plugin
disabled, admission rejected).  The payload is a *real* QQ ``Message`` with a
real ``MessageSegment.markdown`` (single native mention verified in body
content) and a real ``MessageKeyboard`` rebuilt from the frozen spec.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest

from komari_bot.plugins.komari_roulette import (
    CommandReceipt,
    FulfillmentState,
    StorageUnavailableError,
)
from komari_bot.plugins.komari_roulette.qq.delivery import (
    DeliveryOutcome,
    RouletteDelivery,
    SendNotAcceptedError,
)

from .tsk278_support import (
    FakeCommandService,
    FakeSender,
    assert_no_keyboard_segment,
    assert_single_mention_tag,
    claim,
    has_keyboard_segment,
    message_keyboard_rows,
    message_markdown_content,
    projection,
    receipt,
)

if TYPE_CHECKING:
    from komari_bot.plugins.komari_roulette.qq.delivery import (
        PayloadBuilder,
        RuntimeCheck,
    )


class _RuntimeCheckError(RuntimeError):
    """Stand-in for a failing live admission/credential recheck."""


def _delivery(
    *,
    service: FakeCommandService | None = None,
    runtime_check: RuntimeCheck | None = None,
    payload_builder: PayloadBuilder | None = None,
) -> RouletteDelivery:
    return RouletteDelivery(
        service=service or FakeCommandService(),
        runtime_check=runtime_check,
        payload_builder=payload_builder,
    )


def _success_receipt() -> CommandReceipt:
    return receipt(
        reply=projection(
            '> 小明又逃过一劫，获得了啤酒。\n\n**当前：小明** <qqbot-at-user id="member-1" />',
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


class _IdNoneResponse:
    """Real ``PostGroupMessagesReturn`` whose ``id`` is ``None``."""

    id = None


class _IdLessResponse:
    """Return object with no usable platform message id attribute."""


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(None, id="none"),
        pytest.param(_IdNoneResponse(), id="id-none"),
        pytest.param(_IdLessResponse(), id="no-id"),
        pytest.param({"no": "id"}, id="dict-no-id"),
        pytest.param("", id="empty-string"),
    ],
)
async def test_deliver_unusable_platform_id_is_unknown(response: object) -> None:
    """平台回执没有可用 id：发送已发生但无法确认 → UNKNOWN，绝不写假 id。

    真实 ``PostGroupMessagesReturn.id`` 类型是 ``str | None``，缺失/``None`` 回执
    不得被 ``str(...)`` 变成 ``"None"``/``"{...}"`` 后标记 DELIVERED。
    """
    service = FakeCommandService()
    service.claim_result = claim("receipt-1")
    sender = FakeSender(result=response)
    outcome = await _delivery(service=service).deliver(_success_receipt(), sender)

    assert outcome is DeliveryOutcome.UNKNOWN
    assert len(sender.network_calls) == 1
    assert service.mark_delivered_calls == []
    assert service.mark_not_delivered_calls == []


async def test_deliver_object_with_usable_platform_id_is_delivered() -> None:
    """带真实 ``id`` 属性的回执照常标记 DELIVERED 并保存该 id。"""

    class _Response:
        id = "real-platform-id"

    service = FakeCommandService()
    service.claim_result = claim("receipt-1")
    sender = FakeSender(result=_Response())
    outcome = await _delivery(service=service).deliver(_success_receipt(), sender)

    assert outcome is DeliveryOutcome.DELIVERED
    assert service.mark_delivered_calls == [
        (claim("receipt-1"), "real-platform-id")
    ]


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


async def test_deliver_sends_real_qq_message_payload() -> None:
    service = FakeCommandService()
    service.claim_result = claim("receipt-1")
    sender = FakeSender()
    frozen = _success_receipt()
    await _delivery(service=service).deliver(frozen, sender)

    message = sender.calls[0]["message"]
    # 真实 QQ Message：markdown 段正文来自冻结收据，且原生提及在正文正确位置。
    text = message_markdown_content(message)
    assert text == frozen.reply.body
    assert_single_mention_tag(text, "member-1")
    # 冻结 keyboard spec 无按钮：载荷不能携带空 keyboard 字段。
    assert_no_keyboard_segment(message)
    # 送达只读冻结收据：不 observe、不重读当前状态。
    assert service.observe_calls == []
    assert service.execute_calls == []


async def test_deliver_empty_keyboard_sends_markdown_only() -> None:
    """空按钮 spec 不得构造空 keyboard 段（TSK-266 1F “无按钮”）。"""
    service = FakeCommandService()
    service.claim_result = claim("receipt-1")
    sender = FakeSender()
    frozen = receipt(
        reply=projection("> 无按钮正文。", keyboard_spec='{"rows": []}')
    )
    await _delivery(service=service).deliver(frozen, sender)

    message = sender.calls[0]["message"]
    assert_no_keyboard_segment(message)
    assert message_markdown_content(message) == frozen.reply.body


async def test_deliver_sends_real_keyboard_from_frozen_spec() -> None:
    service = FakeCommandService()
    service.claim_result = claim("receipt-1")
    sender = FakeSender()
    spec = json.dumps(
        {"rows": [[{"label": "🔫开枪", "data": "/轮盘 开枪"}]]}
    )
    frozen = receipt(
        reply=projection("> 测试。", keyboard_spec=spec),
    )
    await _delivery(service=service).deliver(frozen, sender)

    message = sender.calls[0]["message"]
    # 有按钮分支：载荷必须真实携带 keyboard 段（不是 message["keyboard"] 假阴性）。
    assert has_keyboard_segment(message) is True
    keyboard_rows = message_keyboard_rows(message)
    assert [[b.label for b in row] for row in keyboard_rows] == [["🔫开枪"]]
    assert keyboard_rows[0][0].data == "/轮盘 开枪"
    assert keyboard_rows[0][0].action_type == 2
    assert keyboard_rows[0][0].permission_type == 2
    assert keyboard_rows[0][0].reply is False
    assert keyboard_rows[0][0].enter is False


# ---------------------------------------------------------------------------
# Ordering: build → claim → runtime recheck → send → mark
# ---------------------------------------------------------------------------


async def test_deliver_order_build_then_claim_then_send_then_mark() -> None:
    events: list[str] = []

    class RecordingService(FakeCommandService):
        async def claim_fulfillment(self, receipt_id: str):
            events.append("claim")
            return await super().claim_fulfillment(receipt_id)

        async def check_fulfillment_window(self, claim: Any) -> bool:
            events.append("check")
            return await super().check_fulfillment_window(claim)

        async def mark_delivered(
            self,
            claim: Any,
            *,
            platform_message_id: str,
        ) -> None:
            events.append("mark_delivered")
            return await super().mark_delivered(
                claim, platform_message_id=platform_message_id
            )

    class RecordingSender(FakeSender):
        async def send_to_group(
            self,
            group_openid: str,
            message: Any,
            *,
            msg_id: str | None = None,
            msg_seq: int | None = None,
            **kwargs: Any,
        ) -> Any:
            events.append("send")
            return await super().send_to_group(
                group_openid,
                message,
                msg_id=msg_id,
                msg_seq=msg_seq,
                **kwargs,
            )

    service = RecordingService()
    service.claim_result = claim("receipt-1")
    delivered_receipt = _success_receipt()

    def recording_builder(_r: CommandReceipt) -> object:
        events.append("build")
        return object()

    def recording_runtime(receipt: CommandReceipt) -> bool:
        # 按调用：重核看到的必须是本次投递的收据，而非进程全局的“当前事件”。
        assert receipt is delivered_receipt
        events.append("runtime_check")
        return True

    await _delivery(
        service=service,
        runtime_check=recording_runtime,
        payload_builder=recording_builder,
    ).deliver(delivered_receipt, RecordingSender())

    # 固定顺序：构建冻结载荷 → 原子领取 → runtime 重核 → 凭证窗口重核 →
    # 发送 → mark_delivered。
    assert events == [
        "build",
        "claim",
        "runtime_check",
        "check",
        "send",
        "mark_delivered",
    ]
    assert service.mark_delivered_calls == [(claim("receipt-1"), "qq-platform-msg-1")]


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


async def test_deliver_runtime_recheck_failure_is_zero_network() -> None:
    """发送前 runtime 最后重核（5 分钟凭证过期/禁用/准入拒绝）失败 → NOT_DELIVERED。

    276 没有 ``mark_not_started_failed``（已核查实际接口），预发送失败经真实
    claim_fulfillment + mark_not_delivered 完成，0 网络调用平台。
    """
    service = FakeCommandService()
    service.claim_result = claim("receipt-1")
    sender = FakeSender()
    delivered_receipt = _success_receipt()

    def deny_runtime(receipt: CommandReceipt) -> bool:
        assert receipt is delivered_receipt
        return False

    outcome = await _delivery(
        service=service,
        runtime_check=deny_runtime,
    ).deliver(delivered_receipt, sender)

    assert outcome is DeliveryOutcome.NOT_DELIVERED
    assert sender.calls == []
    assert sender.network_calls == []
    assert service.claim_calls == ["receipt-1"]
    assert service.mark_not_delivered_calls == [claim("receipt-1")]
    assert service.mark_delivered_calls == []


async def test_deliver_runtime_recheck_runs_after_claim() -> None:
    """runtime_check 必须在 claim 之后、send 之前执行。"""
    service = FakeCommandService()
    service.claim_result = claim("receipt-1")
    sender = FakeSender()
    seen: list[str] = []

    delivered_receipt = _success_receipt()

    def recording_runtime(receipt: CommandReceipt) -> bool:
        assert receipt is delivered_receipt
        seen.append("runtime")
        assert service.claim_calls == ["receipt-1"], (
            "runtime_check must run after claim"
        )
        return True

    await _delivery(service=service, runtime_check=recording_runtime).deliver(
        delivered_receipt, sender
    )
    assert seen == ["runtime"]
    assert len(sender.calls) == 1


async def test_deliver_async_runtime_recheck_is_awaited() -> None:
    """真实准入重核可为异步可调用（返回 Awaitable[bool]）；合同不强制仅同步 bool。"""
    service = FakeCommandService()
    service.claim_result = claim("receipt-1")
    sender = FakeSender()
    seen: list[str] = []

    delivered_receipt = _success_receipt()

    async def async_runtime(receipt: CommandReceipt) -> bool:
        assert receipt is delivered_receipt
        seen.append("runtime")
        return False

    outcome = await _delivery(
        service=service,
        runtime_check=async_runtime,
    ).deliver(delivered_receipt, sender)

    assert outcome is DeliveryOutcome.NOT_DELIVERED
    assert seen == ["runtime"]
    assert sender.calls == []
    assert sender.network_calls == []
    assert service.claim_calls == ["receipt-1"]
    assert service.mark_not_delivered_calls == [claim("receipt-1")]
    assert service.mark_delivered_calls == []


async def test_deliver_sync_runtime_recheck_exception_fails_closed() -> None:
    """runtime 重核查抛异常（发送尚未开始、0 网络）→ NOT_DELIVERED，不留悬挂 PENDING。"""
    service = FakeCommandService()
    service.claim_result = claim("receipt-1")
    sender = FakeSender()

    delivered_receipt = _success_receipt()

    def exploding_runtime(receipt: CommandReceipt) -> bool:
        assert receipt is delivered_receipt
        raise _RuntimeCheckError

    outcome = await _delivery(
        service=service,
        runtime_check=exploding_runtime,
    ).deliver(delivered_receipt, sender)

    assert outcome is DeliveryOutcome.NOT_DELIVERED
    assert sender.calls == []
    assert sender.network_calls == []
    assert service.claim_calls == ["receipt-1"]
    assert service.mark_not_delivered_calls == [claim("receipt-1")]
    assert service.mark_delivered_calls == []


async def test_deliver_async_runtime_recheck_exception_fails_closed() -> None:
    """异步 runtime 重核查抛异常 → 同样故障关闭为 NOT_DELIVERED（0 网络）。"""
    service = FakeCommandService()
    service.claim_result = claim("receipt-1")
    sender = FakeSender()

    delivered_receipt = _success_receipt()

    async def exploding_runtime(receipt: CommandReceipt) -> bool:
        assert receipt is delivered_receipt
        raise _RuntimeCheckError

    outcome = await _delivery(
        service=service,
        runtime_check=exploding_runtime,
    ).deliver(delivered_receipt, sender)

    assert outcome is DeliveryOutcome.NOT_DELIVERED
    assert sender.calls == []
    assert sender.network_calls == []
    assert service.mark_not_delivered_calls == [claim("receipt-1")]
    assert service.mark_delivered_calls == []


# ---------------------------------------------------------------------------
# Authoritative pre-send credential-window recheck (service seam)
# ---------------------------------------------------------------------------


def test_command_service_exposes_async_window_recheck_seam() -> None:
    """契约：``check_fulfillment_window(claim) -> bool`` 是真实异步服务 seam。

    Delivery 必须在发送前直接调用它（不是可选 ``getattr``/本地时钟），
    参数名与协程形状记录为该 seam 的契约。
    """

    import inspect

    from komari_bot.plugins.komari_roulette import RouletteCommandService

    seam = RouletteCommandService.check_fulfillment_window
    assert inspect.iscoroutinefunction(seam)
    assert list(inspect.signature(seam).parameters) == ["self", "claim"]


async def test_deliver_window_recheck_false_is_zero_network_not_delivered() -> None:
    """发送前凭证窗口重核返回 False → 0 网络、mark_not_delivered、NOT_DELIVERED。

    重核只针对有效的 PENDING claim，且不得再次 claim（不能自己制造第二次
    发送权）。
    """
    service = FakeCommandService()
    service.claim_result = claim("receipt-1")
    service.check_result = False
    sender = FakeSender()
    outcome = await _delivery(service=service).deliver(_success_receipt(), sender)

    assert outcome is DeliveryOutcome.NOT_DELIVERED
    assert service.check_calls == [claim("receipt-1")]
    assert service.claim_calls == ["receipt-1"]
    assert sender.calls == []
    assert sender.network_calls == []
    assert service.mark_not_delivered_calls == [claim("receipt-1")]
    assert service.mark_delivered_calls == []


async def test_deliver_window_recheck_exception_fails_closed_zero_network() -> None:
    """窗口重核自身抛异常（PG 时钟/收据不可读）→ 故障关闭，0 网络、NOT_DELIVERED。"""
    service = FakeCommandService()
    service.claim_result = claim("receipt-1")
    service.check_error = _RuntimeCheckError("window read failed")
    sender = FakeSender()
    outcome = await _delivery(service=service).deliver(_success_receipt(), sender)

    assert outcome is DeliveryOutcome.NOT_DELIVERED
    assert service.check_calls == [claim("receipt-1")]
    assert service.claim_calls == ["receipt-1"]
    assert sender.calls == []
    assert sender.network_calls == []
    assert service.mark_not_delivered_calls == [claim("receipt-1")]
    assert service.mark_delivered_calls == []


async def test_deliver_window_recheck_skipped_for_non_pending_claim() -> None:
    """claim 非 PENDING_CONFIRMATION → 直接 NOT_DELIVERED，绝不重核窗口。"""
    service = FakeCommandService()
    service.claim_result = claim("receipt-1", state=FulfillmentState.NOT_DELIVERED)
    sender = FakeSender()
    outcome = await _delivery(service=service).deliver(_success_receipt(), sender)

    assert outcome is DeliveryOutcome.NOT_DELIVERED
    assert service.check_calls == []
    assert service.claim_calls == ["receipt-1"]
    assert sender.calls == []
    assert sender.network_calls == []
    assert service.mark_not_delivered_calls == []
    assert service.mark_delivered_calls == []


async def test_deliver_window_recheck_runs_without_runtime_check() -> None:
    """窗口重核是必选 seam；没有注入 runtime_check 也必须按当前服务状态重核。"""
    service = FakeCommandService()
    service.claim_result = claim("receipt-1")
    sender = FakeSender()
    outcome = await _delivery(service=service).deliver(_success_receipt(), sender)

    assert outcome is DeliveryOutcome.DELIVERED
    assert service.check_calls == [claim("receipt-1")]
    assert service.claim_calls == ["receipt-1"]


# ---------------------------------------------------------------------------
# Credential age fail-closed (claim seam)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [None, object(), [], {}, b"bytes", "not-a-number", True, False],
    ids=["none", "object", "list", "dict", "bytes", "text", "bool-true", "bool-false"],
)
def test_invalid_credential_age_fails_closed(value: object) -> None:
    """不可用的 driver age 不得参与比较：返回 None → claim 侧收敛 NOT_DELIVERED。

    只锁定行为边界（invalid → 不可用/失败关闭），不锁定实现里的具体
    isinstance 编码；asyncpg 给 ``numeric`` 的 Decimal 走真实数值路径。
    """
    from komari_bot.plugins.komari_roulette.command_service import _age_seconds

    assert _age_seconds(value) is None


def test_numeric_credential_age_is_usable_seconds() -> None:
    from decimal import Decimal

    from komari_bot.plugins.komari_roulette.command_service import _age_seconds

    assert _age_seconds(Decimal("299.5")) == 299.5
    assert _age_seconds(0) == 0.0


async def test_deliver_build_failure_after_claim_is_zero_send() -> None:
    """冻结载荷构建失败（如损坏的 keyboard spec）→ 0 发送，收据转 NOT_DELIVERED。"""
    service = FakeCommandService()
    service.claim_result = claim("receipt-1")
    sender = FakeSender()
    broken = receipt(reply=projection("> 测试。", keyboard_spec="{not-json"))

    outcome = await _delivery(service=service).deliver(broken, sender)

    assert outcome is DeliveryOutcome.NOT_DELIVERED
    assert sender.calls == []
    assert sender.network_calls == []
    assert service.claim_calls == ["receipt-1"]
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


async def test_expired_claim_never_sends() -> None:
    """claim 已把过期凭证原子收敛为 NOT_DELIVERED → 立即返回, 0 发送、0 mark。

    TSK-267 §8 / TSK-278 评论 6a9ed97d：5 分钟窗口在 claim 内以收据
    `created_at` 对照 DB 时钟判定；非 PENDING_CONFIRMATION 的 claim 不授权发送。
    """
    service = FakeCommandService()
    service.claim_result = claim("receipt-1", state=FulfillmentState.NOT_DELIVERED)
    sender = FakeSender()

    outcome = await _delivery(service=service).deliver(_success_receipt(), sender)

    assert outcome is DeliveryOutcome.NOT_DELIVERED
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
