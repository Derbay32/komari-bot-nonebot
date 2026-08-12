"""回复履约 workflow 的 contract 阶段领域验收。"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from komari_bot.plugins.komari_memory.services.redis_manager import MessageSchema

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from nonebug import App


class _ParentChildRepository:
    """只保存父送达事实与冻结子项的内存 adapter。"""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}

    @property
    def delivered_ids(self) -> set[str]:
        return {
            fulfillment_id
            for fulfillment_id, record in self.records.items()
            if record["delivery_state"] == "DELIVERED"
        }

    @property
    def pending_confirmation_ids(self) -> set[str]:
        return {
            fulfillment_id
            for fulfillment_id, record in self.records.items()
            if record["delivery_state"] == "PENDING_CONFIRMATION"
        }

    @property
    def not_delivered_ids(self) -> set[str]:
        return {
            fulfillment_id
            for fulfillment_id, record in self.records.items()
            if record["delivery_state"] == "NOT_DELIVERED"
        }

    async def has_active_operation(self, fulfillment_id: str) -> bool:
        return fulfillment_id in self.records

    async def prepare(self, draft: Any) -> bool:
        if draft.fulfillment_id in self.records:
            return False
        self.records[draft.fulfillment_id] = {
            "fulfillment_id": draft.fulfillment_id,
            "delivery_state": "NOT_STARTED",
            "platform_message_id": None,
            "commitments": tuple(draft.commitments),
        }
        return True

    async def mark_send_started(self, fulfillment_id: str) -> bool:
        record = self.records[fulfillment_id]
        if record["delivery_state"] != "NOT_STARTED":
            return False
        record["delivery_state"] = "PENDING_CONFIRMATION"
        return True

    async def mark_delivered(
        self,
        fulfillment_id: str,
        *,
        platform_message_id: str | None = None,
    ) -> bool:
        record = self.records[fulfillment_id]
        if record["delivery_state"] != "PENDING_CONFIRMATION":
            return False
        record["delivery_state"] = "DELIVERED"
        record["platform_message_id"] = platform_message_id
        return True

    async def mark_not_delivered(self, fulfillment_id: str) -> bool:
        record = self.records[fulfillment_id]
        if record["delivery_state"] not in {
            "NOT_STARTED",
            "PENDING_CONFIRMATION",
        }:
            return False
        record["delivery_state"] = "NOT_DELIVERED"
        return True

    async def claim_fresh_not_started(self, **_kwargs: object) -> list[dict[str, Any]]:
        return []

    async def expire_stale_not_started(self, **_kwargs: object) -> list[dict[str, Any]]:
        return []


class _CommitmentWorkflow:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.fulfillment_ids: list[str] = []

    async def recover_fulfillment(self, fulfillment_id: str) -> bool:
        self.events.append("recover_fulfillment")
        self.fulfillment_ids.append(fulfillment_id)
        return True

    async def recover_pending(self) -> int:
        self.events.append("recover_commitments")
        return 0

    async def cleanup_terminal_fulfillments(self) -> int:
        self.events.append("cleanup_terminal_fulfillments")
        return 0


class _AlertService:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def recover_alerts(self, **_kwargs: object) -> int:
        self.events.append("recover_alerts")
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
        adapter_name="onebot.v11",
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
) -> tuple[
    Any,
    _ParentChildRepository,
    _CommitmentWorkflow,
    _AlertService,
    _ProactiveReservation,
    list[str],
]:
    events: list[str] = []
    repository = _ParentChildRepository()
    commitments = _CommitmentWorkflow(events)
    alerts = _AlertService(events)
    proactive = _ProactiveReservation()
    workflow = module.ReplyFulfillmentWorkflow(
        repository=repository,
        proactive_reservation=proactive,
        config_getter=_config,
        recovery_senders_getter=dict,
        commitment_workflow=commitments,
        alert_service=alerts,
    )
    return workflow, repository, commitments, alerts, proactive, events


async def _send_success(_pending: object) -> object:
    module = import_module(
        "komari_bot.plugins.komari_chat.services.reply_fulfillment_workflow"
    )
    return module.ReplyDeliveryResult.delivered("7788")


@pytest.mark.asyncio
async def test_fulfill_persists_delivery_before_delegating_commitments(
    workflow_module: Any,
) -> None:
    workflow, repository, commitments, _alerts, _proactive, events = _workflow(
        workflow_module
    )
    pending = _pending_reply("reply-operation-1")

    assert await workflow.fulfill(pending, send_reply=_send_success) is True

    assert repository.delivered_ids == {pending.operation_id}
    assert repository.records[pending.operation_id]["platform_message_id"] == "7788"
    assert commitments.fulfillment_ids == [pending.operation_id]
    assert events == ["recover_fulfillment", "recover_alerts"]


@pytest.mark.asyncio
async def test_definitive_delivery_failure_terminates_without_commitments(
    workflow_module: Any,
) -> None:
    workflow, repository, commitments, _alerts, proactive, _events = _workflow(
        workflow_module
    )
    reservation = _ReservationHandle()
    pending = _pending_reply("reply-operation-rejected", reservation=reservation)

    async def _send_rejected(_pending: object) -> object:
        return workflow_module.ReplyDeliveryResult.not_delivered()

    assert await workflow.fulfill(pending, send_reply=_send_rejected) is False

    assert repository.not_delivered_ids == {pending.operation_id}
    assert reservation.release_count == 1
    assert proactive.released == []
    assert commitments.fulfillment_ids == []


@pytest.mark.asyncio
async def test_unknown_delivery_is_alerted_without_running_commitments(
    workflow_module: Any,
) -> None:
    workflow, repository, commitments, _alerts, _proactive, events = _workflow(
        workflow_module
    )
    reservation = _ReservationHandle()
    pending = _pending_reply("reply-operation-unknown", reservation=reservation)

    async def _send_unknown(_pending: object) -> object:
        return workflow_module.ReplyDeliveryResult.pending_confirmation()

    assert await workflow.fulfill(pending, send_reply=_send_unknown) is False

    assert repository.pending_confirmation_ids == {pending.operation_id}
    assert reservation.release_count == 0
    assert commitments.fulfillment_ids == []
    assert events == ["recover_alerts"]


@pytest.mark.asyncio
async def test_recover_coordinates_commitments_alerts_and_hourly_cleanup(
    workflow_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, _repository, _commitments, _alerts, _proactive, events = _workflow(
        workflow_module
    )
    clock = iter((3_601.0, 3_602.0))
    monkeypatch.setattr(workflow_module.time, "monotonic", lambda: next(clock))

    assert await workflow.recover_pending() == 0
    assert events == [
        "recover_commitments",
        "recover_alerts",
        "cleanup_terminal_fulfillments",
    ]

    events.clear()
    assert await workflow.recover_pending() == 0
    assert events == ["recover_commitments", "recover_alerts"]


def test_builder_shares_one_parent_child_repository_across_workflow_services(
    workflow_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: dict[str, object] = {}

    class _Repository:
        def __init__(self, pg_pool: object) -> None:
            built["pool"] = pg_pool

    class _Commitments:
        def __init__(self, **kwargs: object) -> None:
            built["commitment_repository"] = kwargs["repository"]

    class _Alerts:
        def __init__(self, **kwargs: object) -> None:
            built["alert_repository"] = kwargs["repository"]
            built["bots_provider"] = kwargs["bots_provider"]
            built["superusers_provider"] = kwargs["superusers_provider"]

    monkeypatch.setattr(workflow_module, "ReplyFulfillmentRepository", _Repository)
    monkeypatch.setattr(workflow_module, "ReplyCommitmentWorkflow", _Commitments)
    monkeypatch.setattr(workflow_module, "ReplyFulfillmentAlertService", _Alerts)
    pg_pool = object()
    bots_provider: Callable[[], Mapping[object, object]] = dict
    superusers_provider: Callable[[], Iterable[object]] = tuple

    workflow = workflow_module.build_reply_fulfillment_workflow(
        pg_pool=pg_pool,
        redis=object(),
        proactive_reservation=object(),
        user_data=object(),
        config_getter=_config,
        recovery_senders_getter=dict,
        bots_provider=bots_provider,
        superusers_provider=superusers_provider,
    )

    assert workflow.repository is built["commitment_repository"]
    assert workflow.repository is built["alert_repository"]
    assert built["pool"] is pg_pool
    assert built["bots_provider"] is bots_provider
    assert built["superusers_provider"] is superusers_provider


def test_message_handler_does_not_own_reply_fulfillment_or_repository() -> None:
    handler_module = import_module(
        "komari_bot.plugins.komari_chat.handlers.message_handler"
    )
    removed_methods = (
        "prepare_pending_reply",
        "cancel_prepared_reply",
        "commit_delivered_reply",
        "retry_pending_reply_commits",
        "_reply_commit_heartbeat",
        "_mark_reply_commit_step",
        "_process_claimed_reply_commit",
        "_finish_claimed_reply_commit",
    )

    for method_name in removed_methods:
        assert not hasattr(handler_module.MessageHandler, method_name)

    source = handler_module.__file__
    assert source is not None
    with Path(source).open(encoding="utf-8") as source_file:
        text = source_file.read()
    assert "reply_commit_repository" not in text
    assert "komari_chat_reply_commit_outbox" not in text
