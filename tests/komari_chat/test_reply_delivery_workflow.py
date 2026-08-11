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

if TYPE_CHECKING:
    from nonebug import App


ReplySender = Callable[[object], Awaitable[object]]
BotIdentity = tuple[str, str]


class _DeliveryRepository:
    """只暴露履约持久事实的内存 adapter。"""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
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
            operation_id
            for operation_id, record in self.records.items()
            if record["status"] == state
        }

    def seed_not_started(
        self,
        operation_id: str,
        *,
        age_seconds: int,
        bot_self_id: str = "bot-1",
        adapter_name: str = "OneBot V11",
    ) -> None:
        self.records[operation_id] = {
            "operation_id": operation_id,
            "status": "NOT_STARTED",
            "created_at": self.now - timedelta(seconds=age_seconds),
            "bot_self_id": bot_self_id,
            "adapter_name": adapter_name,
            "group_id": "group-1",
            "source_message_id": "message-1",
            "reply_target_message_id": "message-1",
            "reply_content": "回复正文",
            "proactive_reservation_id": "reservation-1",
        }

    async def has_active_operation(self, operation_id: str) -> bool:
        return operation_id in self.records

    async def prepare(self, draft: Any) -> bool:
        if draft.fulfillment_id in self.records:
            return False
        self.records[draft.fulfillment_id] = {
            "operation_id": draft.fulfillment_id,
            "status": "NOT_STARTED",
            "created_at": self.now,
            "bot_self_id": draft.bot_self_id,
            "adapter_name": draft.adapter_name,
            "group_id": draft.group_id,
            "source_message_id": draft.trigger_message_id,
            "reply_target_message_id": draft.reply_target_message_id,
            "reply_content": draft.reply_content,
            "proactive_reservation_id": "reservation-1",
        }
        return True

    async def mark_send_started(self, operation_id: str) -> bool:
        record = self.records[operation_id]
        if not self.allow_send_start or record["status"] != "NOT_STARTED":
            return False
        record["status"] = "PENDING_CONFIRMATION"
        return True

    async def mark_delivered(
        self,
        operation_id: str,
        *,
        platform_message_id: str | None = None,
    ) -> bool:
        record = self.records[operation_id]
        if record["status"] == "DELIVERED":
            existing = record.get("platform_message_id")
            if (
                existing is not None
                and platform_message_id is not None
                and existing != platform_message_id
            ):
                msg = "平台消息 ID 冲突"
                raise ValueError(msg)
            return True
        if record["status"] != "PENDING_CONFIRMATION":
            return False
        record["status"] = "DELIVERED"
        record["platform_message_id"] = platform_message_id
        return True

    async def mark_not_delivered(self, operation_id: str) -> bool:
        record = self.records[operation_id]
        if record["status"] not in {"NOT_STARTED", "PENDING_CONFIRMATION"}:
            return record["status"] == "NOT_DELIVERED"
        record["status"] = "NOT_DELIVERED"
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
        for record in self.records.values():
            age = (self.now - record["created_at"]).total_seconds()
            if (
                record["status"] == "NOT_STARTED"
                and record["bot_self_id"] == bot_self_id
                and record["adapter_name"] == adapter_name
                and age < freshness_seconds
                and len(claimed) < limit
            ):
                record["status"] = "PENDING_CONFIRMATION"
                claimed.append(dict(record))
        return claimed

    async def expire_stale_not_started(
        self,
        *,
        freshness_seconds: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        expired: list[dict[str, Any]] = []
        for record in self.records.values():
            age = (self.now - record["created_at"]).total_seconds()
            if (
                record["status"] == "NOT_STARTED"
                and age >= freshness_seconds
                and len(expired) < limit
            ):
                record["status"] = "NOT_DELIVERED"
                expired.append(dict(record))
        return expired

    async def claim_operation(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def claim_pending(self, **_kwargs: object) -> list[dict[str, Any]]:
        return []

    async def cleanup_tombstones(self, **_kwargs: object) -> int:
        return 0


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
        reply_commit_lease_seconds=60,
        reply_commit_max_attempts=5,
        reply_commit_retry_base_seconds=1,
        reply_commit_batch_size=20,
        reply_commit_tombstone_retention_days=30,
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
        operation_id=operation_id,
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
) -> Any:
    return module.ReplyFulfillmentWorkflow(
        repository=repository,
        redis=SimpleNamespace(),
        proactive_reservation=proactive,
        user_data=SimpleNamespace(),
        config_getter=_config,
        recovery_senders_getter=lambda: recovery_senders or {},
    )


async def test_send_capability_observes_persisted_pending_confirmation(
    workflow_module: Any,
) -> None:
    """平台调用发生时，发送开始事实必须已经持久化。"""
    repository = _DeliveryRepository()
    proactive = _ProactiveReservation()
    workflow = _workflow(workflow_module, repository, proactive)
    pending = _pending_reply("reply-send-order")

    async def _send(_reply: object) -> object:
        assert repository.pending_confirmation_ids == {pending.operation_id}
        return workflow_module.ReplyDeliveryResult.delivered("platform-1")

    assert await workflow.fulfill(pending, send_reply=_send) is True
    assert repository.delivered_ids == {pending.operation_id}


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
    assert repository.not_started_ids == {pending.operation_id}


async def test_unknown_delivery_stays_pending_and_is_never_resent(
    workflow_module: Any,
) -> None:
    """平台结果未知只进入待确认，后台恢复不得再次调用发送能力。"""
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
    pending = _pending_reply(pending.operation_id, reservation=reservation)

    async def _send(_reply: object) -> object:
        return workflow_module.ReplyDeliveryResult.pending_confirmation()

    assert await workflow.fulfill(pending, send_reply=_send) is False
    assert repository.pending_confirmation_ids == {pending.operation_id}
    assert reservation.release_count == 0

    await workflow.recover_pending()

    assert recovery_send_count == 0
    assert repository.pending_confirmation_ids == {pending.operation_id}


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


async def test_freshness_boundary_expires_without_sending_and_releases_reservation(
    workflow_module: Any,
) -> None:
    """从准备完成起满 120 秒即失效，终止后释放主动回复预占。"""
    repository = _DeliveryRepository()
    proactive = _ProactiveReservation()
    repository.seed_not_started("reply-expired", age_seconds=120)
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
    assert proactive.released == [("group-1", "reservation-1")]


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

    assert repository.pending_confirmation_ids == {pending.operation_id}
    assert reservation.release_count == 0
