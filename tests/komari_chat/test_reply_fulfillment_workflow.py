"""回复履约 workflow 的 contract 阶段领域验收。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from komari_bot.plugins.komari_memory.services.redis_manager import MessageSchema
from tests.komari_chat.fulfillment_row_keys import PARENT_ROW_KEYS

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from nonebug import App


class _ParentChildRepository:
    """只保存父送达事实与冻结子项的内存 adapter（真实父子表形状）。

    领取/过期返回行精确模拟真实仓储 ``RETURNING parent.*`` 键集，外加
    从主动回复确认承诺子 payload 投影的 ``proactive_group_id`` /
    ``proactive_reservation_id``；恢复路径以真实形状闭环。
    """

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.commitment_payloads: dict[str, dict[str, dict[str, Any] | None]] = {}
        self.now = datetime(2026, 8, 11, tzinfo=UTC)

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

    def _parent_row(
        self,
        draft: Any,
        *,
        prepared_at: datetime,
    ) -> dict[str, Any]:
        return {
            "fulfillment_id": draft.fulfillment_id,
            "payload_hash": draft.payload_hash,
            "request_trace_id": draft.request_trace_id,
            "trigger_message_id": draft.trigger_message_id,
            "trigger_user_id": draft.trigger_user_id,
            "group_id": draft.group_id,
            "bot_self_id": draft.bot_self_id,
            "adapter_name": draft.adapter_name,
            "reply_target_message_id": draft.reply_target_message_id,
            "reply_content": draft.reply_content,
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
        reply_content: str = "恢复补发的回复",
    ) -> None:
        """按真实父表键直接播种一条发送前崩溃遗留的 NOT_STARTED 行。"""
        prepared_at = self.now - timedelta(seconds=age_seconds)
        self.records[fulfillment_id] = {
            "fulfillment_id": fulfillment_id,
            "payload_hash": "a" * 64,
            "request_trace_id": f"trace-{fulfillment_id}",
            "trigger_message_id": "message-1",
            "trigger_user_id": "user-1",
            "group_id": group_id,
            "bot_self_id": bot_self_id,
            "adapter_name": adapter_name,
            "reply_target_message_id": "message-1",
            "reply_content": reply_content,
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
        self.commitment_payloads[fulfillment_id] = {
            "proactive_reply_confirmation": {
                "group_id": group_id,
                "reservation_id": "reservation-1",
                "cooldown_seconds": 300,
            },
            "favorability_adjustment": {
                "user_id": "user-1",
                "delta": 1,
                "reason": "正常互动",
            },
            "assistant_reply_history": {
                "group_id": group_id,
                "bot_nickname": "小鞠",
                "reply_content": reply_content,
                "reply_timestamp": 2.0,
            },
        }

    def _claimed_row(self, fulfillment_id: str) -> dict[str, Any]:
        """按真实仓储返回形状投影：父表键 + 子 payload 预占投影。"""
        record = self.records[fulfillment_id]
        row = {key: record.get(key) for key in PARENT_ROW_KEYS}
        payload = self.commitment_payloads.get(fulfillment_id, {}).get(
            "proactive_reply_confirmation"
        )
        row["proactive_group_id"] = (
            payload.get("group_id") if isinstance(payload, dict) else None
        )
        row["proactive_reservation_id"] = (
            payload.get("reservation_id") if isinstance(payload, dict) else None
        )
        return row

    async def has_fulfillment(self, fulfillment_id: str) -> bool:
        return fulfillment_id in self.records

    async def prepare(self, draft: Any) -> bool:
        if draft.fulfillment_id in self.records:
            return False
        self.records[draft.fulfillment_id] = self._parent_row(
            draft,
            prepared_at=self.now,
        )
        self.commitment_payloads[draft.fulfillment_id] = {
            commitment.commitment_type: commitment.to_json()
            for commitment in draft.commitments
        }
        return True

    async def mark_send_started(self, fulfillment_id: str) -> bool:
        record = self.records[fulfillment_id]
        if record["delivery_state"] != "NOT_STARTED":
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
        if record["delivery_state"] != "PENDING_CONFIRMATION":
            return False
        record["delivery_state"] = "DELIVERED"
        record["platform_message_id"] = platform_message_id
        record["delivered_at"] = self.now
        record["reply_content"] = None
        return True

    async def mark_not_delivered(self, fulfillment_id: str) -> bool:
        record = self.records[fulfillment_id]
        if record["delivery_state"] not in {
            "NOT_STARTED",
            "PENDING_CONFIRMATION",
        }:
            return False
        record["delivery_state"] = "NOT_DELIVERED"
        record["not_delivered_at"] = self.now
        record["reply_content"] = None
        for commitment_type in self.commitment_payloads.get(fulfillment_id, {}):
            self.commitment_payloads[fulfillment_id][commitment_type] = None
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
                # 两段式顺序（与真实仓储同事务一致）：先取预占身份投影，
                # 再翻转未送达终态并抹除父正文与子 payload。
                row = self._claimed_row(fulfillment_id)
                record["delivery_state"] = "NOT_DELIVERED"
                record["not_delivered_at"] = self.now
                record["reply_content"] = None
                for commitment_type in self.commitment_payloads.get(fulfillment_id, {}):
                    self.commitment_payloads[fulfillment_id][commitment_type] = None
                row["delivery_state"] = "NOT_DELIVERED"
                row["not_delivered_at"] = record["not_delivered_at"]
                row["reply_content"] = None
                expired.append(row)
        return expired


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


@dataclass(frozen=True)
class _ReservationHandoff:
    """移交凭据 fake：冻结身份快照（reserve 时刻）+ 记录调用的幂等 release()。"""

    group_id: str = "group-1"
    reservation_id: str = "reservation-1"
    cooldown_seconds: int = 300
    release_calls: list[str] = field(default_factory=list)

    @property
    def release_count(self) -> int:
        return len(self.release_calls)

    async def release(self) -> bool:
        self.release_calls.append(self.reservation_id)
        return len(self.release_calls) == 1


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
    handoff: _ReservationHandoff | None = None,
    reply_content: str = "回复正文",
    favorability_delta: int | None = 1,
) -> Any:
    handler_module = import_module(
        "komari_bot.plugins.komari_chat.handlers.message_handler"
    )
    if handoff is None:
        # 与旧默认（proactive_reservation_id="reservation-1"）等价：
        # 默认带一份 group-1 / reservation-1 / 300 秒快照的移交凭据
        handoff = _ReservationHandoff()
    return handler_module.PendingReply(
        reply=reply_content,
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
            content=reply_content,
            interaction_history={"event": "发言", "result": "回复", "emotion": "平静"},
            favorability_delta=favorability_delta,
            favorability_reason="正常互动",
        ),
        force_reply=False,
        bot_nickname="小鞠",
        bot_self_id="bot-1",
        adapter_name="onebot.v11",
        reason="score",
        reply_score=0.9,
        fulfillment_id=operation_id,
        request_trace_id="chat-message-1",
        reply_timestamp=2.0,
        proactive_reservation_id=handoff.reservation_id,
        proactive_handoff=handoff,
    )


def _workflow(
    module: Any,
    *,
    recovery_senders: Mapping[tuple[str, str], Any] | None = None,
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
        recovery_senders_getter=lambda: recovery_senders or {},
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

    assert repository.delivered_ids == {pending.fulfillment_id}
    assert repository.records[pending.fulfillment_id]["platform_message_id"] == "7788"
    assert commitments.fulfillment_ids == [pending.fulfillment_id]
    assert events == ["recover_fulfillment", "recover_alerts"]


@pytest.mark.asyncio
async def test_definitive_delivery_failure_terminates_without_commitments(
    workflow_module: Any,
) -> None:
    workflow, repository, commitments, _alerts, proactive, _events = _workflow(
        workflow_module
    )
    handoff = _ReservationHandoff()
    pending = _pending_reply("reply-operation-rejected", handoff=handoff)

    async def _send_rejected(_pending: object) -> object:
        return workflow_module.ReplyDeliveryResult.not_delivered()

    assert await workflow.fulfill(pending, send_reply=_send_rejected) is False

    assert repository.not_delivered_ids == {pending.fulfillment_id}
    assert handoff.release_count == 1
    assert proactive.released == []
    assert commitments.fulfillment_ids == []


@pytest.mark.asyncio
async def test_unknown_delivery_is_alerted_without_running_commitments(
    workflow_module: Any,
) -> None:
    workflow, repository, commitments, _alerts, _proactive, events = _workflow(
        workflow_module
    )
    handoff = _ReservationHandoff()
    pending = _pending_reply("reply-operation-unknown", handoff=handoff)

    async def _send_unknown(_pending: object) -> object:
        return workflow_module.ReplyDeliveryResult.pending_confirmation()

    assert await workflow.fulfill(pending, send_reply=_send_unknown) is False

    assert repository.pending_confirmation_ids == {pending.fulfillment_id}
    assert handoff.release_count == 0
    assert commitments.fulfillment_ids == []
    assert events == ["recover_alerts"]


@pytest.mark.asyncio
async def test_commitment_cooldown_comes_from_handoff_snapshot_not_live_config(
    workflow_module: Any,
) -> None:
    """AC-3：承诺载荷的冷却时长来自 reserve 冻结快照（凭据），非现读配置。

    凭据快照 42 秒与当前配置 proactive_cooldown=300 不同，证明承诺
    载荷用的是预占时刻冻结值而非生成/履约时的配置。
    """
    workflow, repository, commitments, _alerts, _proactive, _events = _workflow(
        workflow_module
    )
    handoff = _ReservationHandoff(cooldown_seconds=42)
    pending = _pending_reply("reply-operation-snapshot", handoff=handoff)

    assert await workflow.fulfill(pending, send_reply=_send_success) is True

    payload = repository.commitment_payloads["reply-operation-snapshot"][
        "proactive_reply_confirmation"
    ]
    assert payload is not None
    assert payload["group_id"] == "group-1"
    assert payload["reservation_id"] == "reservation-1"
    assert payload["cooldown_seconds"] == 42
    assert commitments.fulfillment_ids == ["reply-operation-snapshot"]


@pytest.mark.asyncio
async def test_empty_reply_content_releases_handoff_without_sending(
    workflow_module: Any,
) -> None:
    """回复内容为空：不发送、不推进承诺，凭据 release() 被调用（唯一释放入口）。"""
    workflow, repository, commitments, _alerts, _proactive, _events = _workflow(
        workflow_module
    )
    handoff = _ReservationHandoff()
    pending = _pending_reply("reply-operation-empty", handoff=handoff, reply_content="")

    assert await workflow.fulfill(pending, send_reply=_send_success) is False

    assert handoff.release_count == 1
    assert repository.records == {}
    assert commitments.fulfillment_ids == []


@pytest.mark.asyncio
async def test_prepare_failure_releases_handoff_and_propagates(
    workflow_module: Any,
) -> None:
    """prepare 失败（favorability_delta 缺失）：释放凭据后原样上抛。"""
    workflow, repository, _commitments, _alerts, _proactive, _events = _workflow(
        workflow_module
    )
    handoff = _ReservationHandoff()
    pending = _pending_reply(
        "reply-operation-prepare-error",
        handoff=handoff,
        favorability_delta=None,
    )

    with pytest.raises(ValueError):
        await workflow.fulfill(pending, send_reply=_send_success)

    assert handoff.release_count == 1
    assert repository.records == {}


@pytest.mark.asyncio
async def test_duplicate_fulfillment_releases_handoff(
    workflow_module: Any,
) -> None:
    """重复履约（prepare 返回 False）：释放凭据并取消本次发送，不重复推进承诺。"""
    workflow, _repository, commitments, _alerts, _proactive, _events = _workflow(
        workflow_module
    )
    first = _ReservationHandoff()
    pending = _pending_reply("reply-operation-dup", handoff=first)

    assert await workflow.fulfill(pending, send_reply=_send_success) is True

    second_handoff = _ReservationHandoff()
    duplicate = _pending_reply("reply-operation-dup", handoff=second_handoff)
    assert await workflow.fulfill(duplicate, send_reply=_send_success) is False

    assert second_handoff.release_count == 1
    assert first.release_count == 0  # 已送达路径不经过凭据释放（确认通道推进承诺）
    assert commitments.fulfillment_ids == ["reply-operation-dup"]


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


@pytest.mark.asyncio
async def test_recover_pending_restores_pre_send_crash_with_real_row_shape(
    workflow_module: Any,
) -> None:
    """发送前崩溃恢复以真实父表行键闭环：领取→补发→送达→推进承诺。

    恢复路径只消费真实 ``RETURNING parent.*`` 键（``fulfillment_id`` /
    ``trigger_message_id`` 等），任何旧宽表键读取都会以 KeyError 失败。
    """
    sent: list[Any] = []

    async def _sender(reply: object) -> object:
        sent.append(reply)
        return workflow_module.ReplyDeliveryResult.delivered("platform-77")

    workflow, repository, commitments, _alerts, proactive, events = _workflow(
        workflow_module,
        recovery_senders={("bot-1", "OneBot V11"): _sender},
    )
    repository.seed_not_started("reply-recover-closed-loop", age_seconds=5)

    assert await workflow.recover_pending() == 1

    assert repository.delivered_ids == {"reply-recover-closed-loop"}
    record = repository.records["reply-recover-closed-loop"]
    assert record["platform_message_id"] == "platform-77"
    assert record["reply_content"] is None
    assert commitments.fulfillment_ids == ["reply-recover-closed-loop"]
    assert events[0] == "recover_fulfillment"
    assert proactive.released == []
    assert len(sent) == 1
    recovered_reply = sent[0]
    assert recovered_reply.group_id == "group-1"
    assert recovered_reply.reply == "恢复补发的回复"
    assert recovered_reply.reply_to_message_id == "message-1"


@pytest.mark.asyncio
async def test_stale_expiry_release_goes_through_persistence_channel(
    workflow_module: Any,
) -> None:
    """满时效终止的恢复路径经持久化身份通道 release(group_id, reservation_id)。

    恢复路径没有进程句柄/凭据，只能凭父行投影的群与预占 ID 释放；
    凭据 release() 不参与（无对象可调）。
    """
    workflow, repository, _commitments, _alerts, proactive, _events = _workflow(
        workflow_module
    )
    repository.seed_not_started("reply-recover-stale", age_seconds=9_999)

    assert await workflow.recover_pending() == 0

    assert repository.not_delivered_ids == {"reply-recover-stale"}
    assert proactive.released == [("group-1", "reservation-1")]


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
