"""回复履约送达事实与发送前恢复验收测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from importlib import import_module
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from komari_bot.plugins.komari_memory.services.redis_manager import MessageSchema
from tests.komari_chat.fulfillment_row_keys import PARENT_ROW_KEYS

if TYPE_CHECKING:
    from nonebug import App


ReplySender = Callable[[object], Awaitable[object]]
BotIdentity = tuple[str, str]

# 领取/过期返回行在父表键之外附带的预占投影键：从主动回复确认承诺
# 子 payload 投影，与管理对账路径 ``reconcile_not_delivered`` 同名。
_PROJECTION_KEYS = ("proactive_group_id", "proactive_reservation_id")


class _DeliveryRepository:
    """只暴露履约持久事实的内存 adapter（真实父子表形状）。

    领取/过期返回行精确模拟真实仓储 ``RETURNING parent.*`` 键集，外加
    从主动回复确认承诺子 payload 投影的预占身份；工作流读取任何旧
    宽表键（``operation_id`` / ``source_message_id`` / 父表预占列）
    都会以 KeyError 失败。
    """

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.commitments: dict[str, dict[str, dict[str, Any] | None]] = {}
        self.now = datetime(2026, 8, 11, tzinfo=UTC)
        self.allow_send_start = True

    @property
    def not_started_ids(self) -> set[str]:
        return self._ids_in_state("NOT_STARTED")

    @property
    def pending_confirmation_ids(self) -> set[str]:
        return self._ids_in_state("PENDING_CONFIRMATION")

    @property
    def delivered_ids(self) -> set[str]:
        return self._ids_in_state("DELIVERED")

    @property
    def not_delivered_ids(self) -> set[str]:
        return self._ids_in_state("NOT_DELIVERED")

    def _ids_in_state(self, state: str) -> set[str]:
        return {
            fulfillment_id
            for fulfillment_id, record in self.records.items()
            if record["delivery_state"] == state
        }

    def _parent_row(
        self,
        fulfillment_id: str,
        *,
        prepared_at: datetime,
        draft: Any = None,
        bot_self_id: str = "bot-1",
        adapter_name: str = "OneBot V11",
        group_id: str = "group-1",
        trigger_message_id: str = "message-1",
        reply_target_message_id: str = "message-1",
        reply_content: str = "回复正文",
    ) -> dict[str, Any]:
        return {
            "fulfillment_id": fulfillment_id,
            "payload_hash": (
                draft.payload_hash if draft is not None else "a" * 64
            ),
            "request_trace_id": (
                draft.request_trace_id if draft is not None else "trace-1"
            ),
            "trigger_message_id": (
                draft.trigger_message_id if draft is not None else trigger_message_id
            ),
            "trigger_user_id": (
                draft.trigger_user_id if draft is not None else "user-1"
            ),
            "group_id": draft.group_id if draft is not None else group_id,
            "bot_self_id": draft.bot_self_id if draft is not None else bot_self_id,
            "adapter_name": (
                draft.adapter_name if draft is not None else adapter_name
            ),
            "reply_target_message_id": (
                draft.reply_target_message_id
                if draft is not None
                else reply_target_message_id
            ),
            "reply_content": (
                draft.reply_content if draft is not None else reply_content
            ),
            "delivery_state": "NOT_STARTED",
            "platform_message_id": None,
            "prepared_at": prepared_at,
            "send_started_at": None,
            "delivered_at": None,
            "not_delivered_at": None,
            "lease_owner": None,
            "lease_expires_at": None,
            "completed_at": None,
            "created_at": prepared_at,
            "updated_at": prepared_at,
            "idempotency_evidence_cleared_at": None,
            "pending_confirmation_alerted_at": None,
        }

    def seed_not_started(
        self,
        fulfillment_id: str,
        *,
        age_seconds: int,
        bot_self_id: str = "bot-1",
        adapter_name: str = "OneBot V11",
        group_id: str = "group-1",
        proactive: tuple[str, str] | None = ("group-1", "reservation-1"),
    ) -> None:
        prepared_at = self.now - timedelta(seconds=age_seconds)
        self.records[fulfillment_id] = self._parent_row(
            fulfillment_id,
            prepared_at=prepared_at,
            bot_self_id=bot_self_id,
            adapter_name=adapter_name,
            group_id=group_id,
        )
        commitments: dict[str, dict[str, Any] | None] = {
            "favorability_adjustment": {
                "user_id": "user-1",
                "delta": 1,
                "reason": "正常互动",
            },
            "assistant_reply_history": {
                "group_id": group_id,
                "bot_nickname": "小鞠",
                "reply_content": "回复正文",
                "reply_timestamp": 2.0,
            },
        }
        if proactive is not None:
            proactive_group_id, reservation_id = proactive
            commitments["proactive_reply_confirmation"] = {
                "group_id": proactive_group_id,
                "reservation_id": reservation_id,
                "cooldown_seconds": 300,
            }
        self.commitments[fulfillment_id] = commitments

    def _proactive_projection(self, fulfillment_id: str) -> tuple[Any, Any]:
        payload = self.commitments.get(fulfillment_id, {}).get(
            "proactive_reply_confirmation"
        )
        if not isinstance(payload, dict):
            return None, None
        return payload.get("group_id"), payload.get("reservation_id")

    def _claimed_row(self, fulfillment_id: str) -> dict[str, Any]:
        """按真实仓储返回形状投影：宽松取键，缺失键留给工作流投影报错。"""
        record = self.records[fulfillment_id]
        row = {key: record.get(key) for key in PARENT_ROW_KEYS}
        group_id, reservation_id = self._proactive_projection(fulfillment_id)
        row["proactive_group_id"] = group_id
        row["proactive_reservation_id"] = reservation_id
        return row

    async def has_active_operation(self, fulfillment_id: str) -> bool:
        return fulfillment_id in self.records

    async def prepare(self, draft: Any) -> bool:
        if draft.fulfillment_id in self.records:
            return False
        self.records[draft.fulfillment_id] = self._parent_row(
            draft.fulfillment_id,
            prepared_at=self.now,
            draft=draft,
        )
        self.commitments[draft.fulfillment_id] = {
            commitment.commitment_type: commitment.to_json()
            for commitment in draft.commitments
        }
        return True

    async def mark_send_started(self, fulfillment_id: str) -> bool:
        record = self.records[fulfillment_id]
        if not self.allow_send_start or record["delivery_state"] != "NOT_STARTED":
            return False
        record["delivery_state"] = "PENDING_CONFIRMATION"
        record["send_started_at"] = self.now
        return True

    async def mark_delivered(
        self,
        fulfillment_id: str,
        *,
        platform_message_id: str | None = None,
    ) -> bool:
        record = self.records[fulfillment_id]
        if record["delivery_state"] == "DELIVERED":
            existing = record.get("platform_message_id")
            if (
                existing is not None
                and platform_message_id is not None
                and existing != platform_message_id
            ):
                msg = "平台消息 ID 冲突"
                raise ValueError(msg)
            return True
        if record["delivery_state"] != "PENDING_CONFIRMATION":
            return False
        record["delivery_state"] = "DELIVERED"
        record["platform_message_id"] = platform_message_id
        record["delivered_at"] = self.now
        record["reply_content"] = None
        return True

    async def mark_not_delivered(self, fulfillment_id: str) -> bool:
        record = self.records[fulfillment_id]
        if record["delivery_state"] not in {"NOT_STARTED", "PENDING_CONFIRMATION"}:
            return False
        record["delivery_state"] = "NOT_DELIVERED"
        record["not_delivered_at"] = self.now
        record["reply_content"] = None
        for commitment_type in self.commitments.get(fulfillment_id, {}):
            self.commitments[fulfillment_id][commitment_type] = None
        return True

    async def claim_fresh_not_started(
        self,
        *,
        bot_self_id: str,
        adapter_name: str,
        freshness_seconds: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        claimed: list[dict[str, Any]] = []
        for fulfillment_id, record in self.records.items():
            age = (self.now - record["prepared_at"]).total_seconds()
            if (
                record["delivery_state"] == "NOT_STARTED"
                and record["bot_self_id"] == bot_self_id
                and record["adapter_name"] == adapter_name
                and age < freshness_seconds
                and len(claimed) < limit
            ):
                record["delivery_state"] = "PENDING_CONFIRMATION"
                record["send_started_at"] = self.now
                claimed.append(self._claimed_row(fulfillment_id))
        return claimed

    async def expire_stale_not_started(
        self,
        *,
        freshness_seconds: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        expired: list[dict[str, Any]] = []
        for fulfillment_id, record in self.records.items():
            age = (self.now - record["prepared_at"]).total_seconds()
            if (
                record["delivery_state"] == "NOT_STARTED"
                and age >= freshness_seconds
                and len(expired) < limit
            ):
                # 两段式顺序（与真实仓储同事务一致）：先在清除子 payload
                # 之前取出预占身份，再翻转未送达终态并抹除父正文与子载荷。
                group_id, reservation_id = self._proactive_projection(fulfillment_id)
                record["delivery_state"] = "NOT_DELIVERED"
                record["not_delivered_at"] = self.now
                record["reply_content"] = None
                for commitment_type in self.commitments.get(fulfillment_id, {}):
                    self.commitments[fulfillment_id][commitment_type] = None
                row = {key: record.get(key) for key in PARENT_ROW_KEYS}
                row["proactive_group_id"] = group_id
                row["proactive_reservation_id"] = reservation_id
                expired.append(row)
        return expired


class _ProactiveReservation:
    def __init__(self) -> None:
        self.released: list[tuple[str, str]] = []

    async def release(self, group_id: str, reservation_id: str) -> None:
        self.released.append((group_id, reservation_id))


class _ReservationHandle:
    def __init__(self) -> None:
        self.release_count = 0

    async def release(self) -> None:
        self.release_count += 1


@pytest.fixture
def workflow_module(app: App) -> Any:
    del app
    return import_module(
        "komari_bot.plugins.komari_chat.services.reply_fulfillment_workflow"
    )


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        proactive_cooldown=300,
        global_interaction_enabled=True,
        global_interaction_trigger_size=20,
        reply_fulfillment_batch_size=20,
        reply_fulfillment_lease_seconds=60,
        reply_fulfillment_max_attempts=5,
        reply_fulfillment_retry_base_seconds=1,
        reply_fulfillment_retry_max_seconds=3600,
        reply_fulfillment_tombstone_retention_days=30,
        reply_fulfillment_freshness_seconds=120,
    )


def _pending_reply(
    operation_id: str,
    *,
    reservation: _ReservationHandle | None = None,
) -> Any:
    handler_module = import_module(
        "komari_bot.plugins.komari_chat.handlers.message_handler"
    )
    return handler_module.PendingReply(
        reply="回复正文",
        reply_to_message_id="message-1",
        message=MessageSchema(
            user_id="user-1",
            user_nickname="测试用户",
            group_id="group-1",
            content="用户正文",
            timestamp=1.0,
            message_id="message-1",
        ),
        reply_result=handler_module.ReplyResult(
            content="回复正文",
            interaction_history={"event": "发言", "result": "回复", "emotion": "平静"},
            favorability_delta=1,
            favorability_reason="正常互动",
        ),
        force_reply=False,
        bot_nickname="小鞠",
        bot_self_id="bot-1",
        adapter_name="OneBot V11",
        reason="score",
        reply_score=0.9,
        fulfillment_id=operation_id,
        request_trace_id="chat-message-1",
        reply_timestamp=2.0,
        proactive_reservation_id="reservation-1",
        proactive_reservation=reservation,
    )


def _workflow(
    module: Any,
    repository: _DeliveryRepository,
    proactive: _ProactiveReservation,
    *,
    recovery_senders: Mapping[BotIdentity, ReplySender] | None = None,
    commitment_workflow: Any = None,
    alert_service: Any = None,
) -> Any:
    return module.ReplyFulfillmentWorkflow(
        repository=repository,
        proactive_reservation=proactive,
        config_getter=_config,
        recovery_senders_getter=lambda: recovery_senders or {},
        commitment_workflow=commitment_workflow
        or SimpleNamespace(
            recover_fulfillment=_noop_recover_fulfillment,
            recover_pending=_noop_recover_pending,
            cleanup_terminal_fulfillments=_noop_recover_pending,
        ),
        alert_service=alert_service
        or SimpleNamespace(recover_alerts=_noop_recover_pending),
    )


async def _noop_recover_fulfillment(_fulfillment_id: str) -> bool:
    return True


async def _noop_recover_pending(**_kwargs: object) -> int:
    return 0


async def test_send_capability_observes_persisted_pending_confirmation(
    workflow_module: Any,
) -> None:
    """平台调用发生时，发送开始事实必须已经持久化。"""
    repository = _DeliveryRepository()
    proactive = _ProactiveReservation()
    workflow = _workflow(workflow_module, repository, proactive)
    pending = _pending_reply("reply-send-order")

    async def _send(_reply: object) -> object:
        assert repository.pending_confirmation_ids == {pending.fulfillment_id}
        return workflow_module.ReplyDeliveryResult.delivered("platform-1")

    assert await workflow.fulfill(pending, send_reply=_send) is True
    assert repository.delivered_ids == {pending.fulfillment_id}


async def test_send_never_starts_when_persistent_start_transition_fails(
    workflow_module: Any,
) -> None:
    """发送开始事实未能持久化时，workflow 绝不调用平台。"""
    repository = _DeliveryRepository()
    repository.allow_send_start = False
    workflow = _workflow(workflow_module, repository, _ProactiveReservation())
    pending = _pending_reply("reply-start-rejected")
    send_count = 0

    async def _send(_reply: object) -> object:
        nonlocal send_count
        send_count += 1
        return workflow_module.ReplyDeliveryResult.delivered()

    with pytest.raises(RuntimeError, match="发送开始"):
        await workflow.fulfill(pending, send_reply=_send)

    assert send_count == 0
    assert repository.not_started_ids == {pending.fulfillment_id}


async def test_unknown_delivery_stays_pending_and_is_never_resent(
    workflow_module: Any,
) -> None:
    """登记发送开始后崩溃：保守待确认，后台恢复不得再次调用发送能力。"""
    repository = _DeliveryRepository()
    proactive = _ProactiveReservation()
    recovery_send_count = 0

    async def _recovery_send(_reply: object) -> object:
        nonlocal recovery_send_count
        recovery_send_count += 1
        return workflow_module.ReplyDeliveryResult.delivered()

    workflow = _workflow(
        workflow_module,
        repository,
        proactive,
        recovery_senders={("bot-1", "OneBot V11"): _recovery_send},
    )
    pending = _pending_reply("reply-unknown")
    reservation = _ReservationHandle()
    pending = _pending_reply(pending.fulfillment_id, reservation=reservation)

    async def _send(_reply: object) -> object:
        return workflow_module.ReplyDeliveryResult.pending_confirmation()

    assert await workflow.fulfill(pending, send_reply=_send) is False
    assert repository.pending_confirmation_ids == {pending.fulfillment_id}
    assert reservation.release_count == 0

    await workflow.recover_pending()

    assert recovery_send_count == 0
    assert repository.pending_confirmation_ids == {pending.fulfillment_id}


async def test_not_started_reply_recovers_only_with_exact_original_bot(
    workflow_module: Any,
) -> None:
    """新鲜回复只允许冻结的 Bot 与适配器恢复发送。"""
    repository = _DeliveryRepository()
    proactive = _ProactiveReservation()
    repository.seed_not_started("reply-fresh", age_seconds=119)
    wrong_send_count = 0

    async def _wrong_sender(_reply: object) -> object:
        nonlocal wrong_send_count
        wrong_send_count += 1
        return workflow_module.ReplyDeliveryResult.delivered("wrong")

    workflow = _workflow(
        workflow_module,
        repository,
        proactive,
        recovery_senders={("bot-2", "OneBot V11"): _wrong_sender},
    )
    await workflow.recover_pending()
    assert wrong_send_count == 0
    assert repository.not_started_ids == {"reply-fresh"}

    sent_requests: list[object] = []

    async def _original_sender(reply: object) -> object:
        sent_requests.append(reply)
        return workflow_module.ReplyDeliveryResult.delivered("platform-2")

    workflow = _workflow(
        workflow_module,
        repository,
        proactive,
        recovery_senders={("bot-1", "OneBot V11"): _original_sender},
    )
    await workflow.recover_pending()

    assert len(sent_requests) == 1
    assert repository.delivered_ids == {"reply-fresh"}
    assert repository.records["reply-fresh"]["platform_message_id"] == "platform-2"


async def test_recovered_delivery_persists_fact_and_advances_commitments(
    workflow_module: Any,
) -> None:
    """持久准备后崩溃：恢复补发成功、送达事实持久化并立即推进承诺。

    恢复 sender 收到的发送载荷必须投影自真实父表列：群、正文与引用
    目标分别来自 ``group_id`` / ``reply_content`` /
    ``reply_target_message_id``。
    """
    repository = _DeliveryRepository()
    proactive = _ProactiveReservation()
    repository.seed_not_started(
        "reply-recovered-delivered",
        age_seconds=5,
        group_id="group-7",
        proactive=("group-7", "reservation-7"),
    )
    commitment_calls: list[str] = []

    async def _recover_fulfillment(fulfillment_id: str) -> bool:
        commitment_calls.append(fulfillment_id)
        return True

    sent: list[Any] = []

    async def _sender(reply: object) -> object:
        sent.append(reply)
        return workflow_module.ReplyDeliveryResult.delivered("platform-9")

    workflow = _workflow(
        workflow_module,
        repository,
        proactive,
        recovery_senders={("bot-1", "OneBot V11"): _sender},
        commitment_workflow=SimpleNamespace(
            recover_fulfillment=_recover_fulfillment,
            recover_pending=_noop_recover_pending,
            cleanup_terminal_fulfillments=_noop_recover_pending,
        ),
    )
    assert await workflow.recover_pending() == 1

    assert repository.delivered_ids == {"reply-recovered-delivered"}
    record = repository.records["reply-recovered-delivered"]
    assert record["platform_message_id"] == "platform-9"
    assert record["reply_content"] is None
    assert commitment_calls == ["reply-recovered-delivered"]
    assert proactive.released == []

    assert len(sent) == 1
    recovered_reply = sent[0]
    assert recovered_reply.group_id == "group-7"
    assert recovered_reply.reply == "回复正文"
    assert recovered_reply.reply_to_message_id == "message-1"


async def test_recovered_not_delivered_terminates_and_releases_reservation(
    workflow_module: Any,
) -> None:
    """恢复发送被平台明确拒绝：转未送达终态并真实释放持久预占。

    预占身份（群 + 预占 ID）来自主动回复确认承诺子 payload 的投影，
    不是任何父表列。
    """
    repository = _DeliveryRepository()
    proactive = _ProactiveReservation()
    repository.seed_not_started(
        "reply-recovered-rejected",
        age_seconds=5,
        group_id="group-9",
        proactive=("group-9", "reservation-9"),
    )

    async def _sender(_reply: object) -> object:
        return workflow_module.ReplyDeliveryResult.not_delivered()

    workflow = _workflow(
        workflow_module,
        repository,
        proactive,
        recovery_senders={("bot-1", "OneBot V11"): _sender},
    )
    assert await workflow.recover_pending() == 0

    assert repository.not_delivered_ids == {"reply-recovered-rejected"}
    assert repository.records["reply-recovered-rejected"]["reply_content"] is None
    assert proactive.released == [("group-9", "reservation-9")]


async def test_recovered_payload_projection_errors_propagate(
    workflow_module: Any,
) -> None:
    """持久载荷缺失属于编程/数据错误，不得伪装成平台结果未知。"""
    repository = _DeliveryRepository()
    proactive = _ProactiveReservation()
    repository.seed_not_started("reply-broken", age_seconds=1)
    repository.records["reply-broken"].pop("reply_content")
    send_count = 0

    async def _sender(_reply: object) -> object:
        nonlocal send_count
        send_count += 1
        return workflow_module.ReplyDeliveryResult.delivered()

    workflow = _workflow(
        workflow_module,
        repository,
        proactive,
        recovery_senders={("bot-1", "OneBot V11"): _sender},
    )

    with pytest.raises(KeyError, match="reply_content"):
        await workflow.recover_pending()

    assert send_count == 0
    assert repository.pending_confirmation_ids == {"reply-broken"}


async def test_freshness_boundary_expires_without_sending_and_releases_reservation(
    workflow_module: Any,
) -> None:
    """从准备完成起满 120 秒即失效：终止后真实释放预占且载荷已清除。"""
    repository = _DeliveryRepository()
    proactive = _ProactiveReservation()
    repository.seed_not_started(
        "reply-expired",
        age_seconds=120,
        group_id="group-5",
        proactive=("group-5", "reservation-5"),
    )
    send_count = 0

    async def _sender(_reply: object) -> object:
        nonlocal send_count
        send_count += 1
        return workflow_module.ReplyDeliveryResult.delivered()

    workflow = _workflow(
        workflow_module,
        repository,
        proactive,
        recovery_senders={("bot-1", "OneBot V11"): _sender},
    )
    await workflow.recover_pending()

    assert send_count == 0
    assert repository.not_delivered_ids == {"reply-expired"}
    assert proactive.released == [("group-5", "reservation-5")]
    # 过期终止的同事务已抹除父正文与全部子 payload。
    assert repository.records["reply-expired"]["reply_content"] is None
    assert all(
        payload is None for payload in repository.commitments["reply-expired"].values()
    )


async def test_expired_reply_without_proactive_child_releases_nothing(
    workflow_module: Any,
) -> None:
    """无主动预占子项的过期回复终止时不调用预占释放。"""
    repository = _DeliveryRepository()
    proactive = _ProactiveReservation()
    repository.seed_not_started("reply-expired-forced", age_seconds=200, proactive=None)

    workflow = _workflow(
        workflow_module,
        repository,
        proactive,
        recovery_senders={},
    )
    await workflow.recover_pending()

    assert repository.not_delivered_ids == {"reply-expired-forced"}
    assert proactive.released == []


async def test_claim_and_expire_rows_match_real_parent_key_set(
    workflow_module: Any,
) -> None:
    """fake 领取/过期返回行只含真实 ``RETURNING parent.*`` 键与预占投影键。

    与真实仓储集成测试互证形状契约：工作流消费任何旧宽表键
    （``operation_id`` / ``source_message_id`` / 父表预占列）都会
    KeyError，恢复路径以此闭环。
    """
    del workflow_module
    repository = _DeliveryRepository()
    repository.seed_not_started("reply-keys-fresh", age_seconds=1)
    repository.seed_not_started("reply-keys-stale", age_seconds=999)

    claimed = await repository.claim_fresh_not_started(
        bot_self_id="bot-1",
        adapter_name="OneBot V11",
        freshness_seconds=120,
        limit=20,
    )
    expired = await repository.expire_stale_not_started(
        freshness_seconds=120,
        limit=20,
    )

    expected = set(PARENT_ROW_KEYS) | set(_PROJECTION_KEYS)
    assert len(claimed) == 1
    assert set(claimed[0]) == expected
    assert claimed[0]["fulfillment_id"] == "reply-keys-fresh"
    assert claimed[0]["proactive_group_id"] == "group-1"
    assert claimed[0]["proactive_reservation_id"] == "reservation-1"
    assert len(expired) == 1
    assert set(expired[0]) == expected
    assert expired[0]["fulfillment_id"] == "reply-keys-stale"
    assert expired[0]["reply_content"] is None


async def test_cancellation_after_send_started_propagates_without_release(
    workflow_module: Any,
) -> None:
    """取消原样传播，发送结果按未知处理且不释放预占。"""
    repository = _DeliveryRepository()
    proactive = _ProactiveReservation()
    workflow = _workflow(workflow_module, repository, proactive)
    reservation = _ReservationHandle()
    pending = _pending_reply("reply-cancelled", reservation=reservation)

    async def _cancel(_reply: object) -> object:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await workflow.fulfill(pending, send_reply=_cancel)

    assert repository.pending_confirmation_ids == {pending.fulfillment_id}
    assert reservation.release_count == 0
