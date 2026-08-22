"""TSK-228 回复履约准入验收共享基础设施（测试专用，不承载生产语义）。

按 ``test_chat_seam_admission`` 的「真实生产函数 + Fake 依赖注入」模式，为
驱动 ``ReplyFulfillmentWorkflow`` / ``ReplyCommitmentWorkflow`` 与真实领域
构建函数提供内存 Fake adapter 与可复用装配 helper。这些 Fake 只承担依赖隔
离，测试断言的目标始终是真实生产对象的返回与副作用（ADR-0012 / TSK-225）。

当前生产 ``ReplyFulfillmentWorkflow`` / ``ReplyCommitmentWorkflow`` 尚未接入
准入（未在任一阶段调用顶层 ``adjudicate``），因此受限态断言以红态失败终止，
证明接入缺失。
"""

from __future__ import annotations

from datetime import UTC, datetime
from importlib import import_module
from types import SimpleNamespace
from typing import Any


def module(name: str) -> Any:
    return import_module(name)


def config() -> SimpleNamespace:
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


def admitted_intents(scripted: Any) -> list[str]:
    """返回脚本裁决记录中的 intent 值序列（断言每个时点的 intent）。"""
    return [intent.value for (_groups, intent) in scripted.calls]


class ParentChildRepository:
    """保存父送达事实与冻结子项的内存 adapter（真实父子表形状）。"""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.commitment_payloads: dict[str, dict[str, dict[str, Any] | None]] = {}
        self.now = datetime(2026, 8, 22, 5, 0, tzinfo=UTC)

    def _ids_for_state(self, state: str) -> set[str]:
        return {
            fid
            for fid, record in self.records.items()
            if record["delivery_state"] == state
        }

    @property
    def not_started_ids(self) -> set[str]:
        return self._ids_for_state("NOT_STARTED")

    @property
    def delivered_ids(self) -> set[str]:
        return self._ids_for_state("DELIVERED")

    @property
    def pending_confirmation_ids(self) -> set[str]:
        return self._ids_for_state("PENDING_CONFIRMATION")

    @property
    def not_delivered_ids(self) -> set[str]:
        return self._ids_for_state("NOT_DELIVERED")

    def _parent_row(self, draft: Any, *, prepared_at: datetime) -> dict[str, Any]:
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
            "proactive_group_id": None,
            "proactive_reservation_id": None,
            "prepared_at": prepared_at,
            "send_started_at": None,
            "delivered_at": None,
            "not_delivered_at": None,
            "completed_at": None,
        }

    def seed_entry_only(
        self,
        fulfillment_id: str,
        *,
        group_id: str = "12345",
        reserved: bool = False,
    ) -> None:
        """播种一条「准备前受限」的最小未送达身份（无正文、无承诺载荷）。"""
        self.records[fulfillment_id] = {
            "fulfillment_id": fulfillment_id,
            "payload_hash": "a" * 64,
            "request_trace_id": f"trace-{fulfillment_id}",
            "trigger_message_id": "message-z",
            "trigger_user_id": "user-1",
            "group_id": group_id,
            "bot_self_id": "bot-1",
            "adapter_name": "OneBot V11",
            "reply_target_message_id": "message-z",
            "reply_content": "",
            "delivery_state": "NOT_STARTED",
            "platform_message_id": None,
            "proactive_group_id": group_id if reserved else None,
            "proactive_reservation_id": "reservation-1" if reserved else None,
            "prepared_at": self.now,
            "send_started_at": None,
            "delivered_at": None,
            "not_delivered_at": None,
            "completed_at": None,
        }

    async def has_fulfillment(self, fulfillment_id: str) -> bool:
        return fulfillment_id in self.records

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
        return True

    async def mark_not_delivered(self, fulfillment_id: str) -> bool:
        record = self.records[fulfillment_id]
        record["delivery_state"] = "NOT_DELIVERED"
        record["not_delivered_at"] = self.now
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
            if len(claimed) >= limit:
                break
            age = (self.now - record["prepared_at"]).total_seconds()
            if (
                record["delivery_state"] == "NOT_STARTED"
                and record["bot_self_id"] == bot_self_id
                and record["adapter_name"] == adapter_name
                and age < freshness_seconds
            ):
                record["delivery_state"] = "PENDING_CONFIRMATION"
                claimed.append(dict(record))
        return claimed

    def seed(
        self,
        fulfillment_id: str,
        *,
        bot_self_id: str = "bot1",
        adapter_name: str = "OneBot V11",
        group_id: str = "group-1",
    ) -> None:
        """播种一条发送前崩溃遗留的带正文 NOT_STARTED 行。"""
        self.records[fulfillment_id] = {
            "fulfillment_id": fulfillment_id,
            "payload_hash": "a" * 64,
            "request_trace_id": f"trace-{fulfillment_id}",
            "trigger_message_id": "message-m",
            "trigger_user_id": "user-1",
            "group_id": group_id,
            "bot_self_id": bot_self_id,
            "adapter_name": adapter_name,
            "reply_target_message_id": "message-m",
            "reply_content": "正文",
            "delivery_state": "NOT_STARTED",
            "platform_message_id": None,
            "proactive_group_id": None,
            "proactive_reservation_id": None,
            "prepared_at": self.now,
            "send_started_at": None,
            "delivered_at": None,
            "not_delivered_at": None,
            "completed_at": None,
        }

    async def expire_stale_not_started(
        self,
        *,
        freshness_seconds: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        expired: list[dict[str, Any]] = []
        for record in self.records.values():
            if len(expired) >= limit:
                break
            age = (self.now - record["prepared_at"]).total_seconds()
            if record["delivery_state"] == "NOT_STARTED" and age >= freshness_seconds:
                record["delivery_state"] = "NOT_DELIVERED"
                expired.append(dict(record))
        return expired

    async def prepare(self, draft: Any) -> bool:
        if draft.fulfillment_id in self.records:
            return False
        self.records[draft.fulfillment_id] = self._parent_row(
            draft, prepared_at=self.now
        )
        self.commitment_payloads[draft.fulfillment_id] = {
            commitment.commitment_type: commitment.to_json()
            for commitment in draft.commitments
        }
        return True

    async def prepare_minimal_identity(
        self,
        fulfillment_id: str,
        *,
        group_id: str,
        request_trace_id: str,
        trigger_message_id: str,
        trigger_user_id: str,
        bot_self_id: str,
        adapter_name: str,
        reply_target_message_id: str,
        payload_hash: str,
    ) -> None:
        """受限准备：只落父身份、不冻结任何承诺（与真实仓库同形）。"""
        if fulfillment_id in self.records:
            return
        self.records[fulfillment_id] = {
            "fulfillment_id": fulfillment_id,
            "payload_hash": payload_hash,
            "request_trace_id": request_trace_id,
            "trigger_message_id": trigger_message_id,
            "trigger_user_id": trigger_user_id,
            "group_id": group_id,
            "bot_self_id": bot_self_id,
            "adapter_name": adapter_name,
            "reply_target_message_id": reply_target_message_id,
            "reply_content": "",
            "delivery_state": "NOT_STARTED",
            "platform_message_id": None,
            "proactive_group_id": None,
            "proactive_reservation_id": None,
            "prepared_at": self.now,
            "send_started_at": None,
            "delivered_at": None,
            "not_delivered_at": None,
            "completed_at": None,
        }
class ReservationHandoff:
    """移交凭据：冻结身份快照 + 幂等 release() 记录。"""

    def __init__(self, cooldown_seconds: int = 300) -> None:
        self.cooldown_seconds = cooldown_seconds
        self.reservation_id = "reservation-1"
        self.group_id = "group-1"
        self.release_calls: list[str] = []

    @property
    def released(self) -> int:
        return len(self.release_calls)

    async def release(self) -> bool:
        self.release_calls.append(self.reservation_id)
        return len(self.release_calls) == 1


class ActiveReservationSpy:
    """预占服务替身：只记录 release / confirm 调用。"""

    def __init__(self) -> None:
        self.released: list[tuple[str, str]] = []
        self.confirmed: list[tuple[str, str, int]] = []

    async def release(self, group_id: str, reservation_id: str) -> None:
        self.released.append((group_id, reservation_id))

    async def confirm(
        self, group_id: str, reservation_id: str, *, cooldown_seconds: int
    ) -> None:
        self.confirmed.append((group_id, reservation_id, cooldown_seconds))


class AlertSpy:
    def __init__(self, called: list[bool]) -> None:
        self.called = called

    async def recover_alerts(self, **_: object) -> int:
        self.called.append(True)
        return 0


def build_fulfillment_workflow(
    *,
    repository: ParentChildRepository,
    recovery_senders: dict[tuple[str, str], Any] | None = None,
) -> tuple[Any, list[bool], list[str]]:
    """装配真实 ``ReplyFulfillmentWorkflow``，注入 Fake 承诺执行器 / 预占 / 告警。"""
    workflow_module = module(
        "komari_bot.plugins.komari_chat.services.reply_fulfillment_workflow"
    )
    called: list[bool] = []
    commitment_ids: list[str] = []
    resolution_spy = ActiveReservationSpy()

    class _Commitments:
        def __init__(self, _ids: list[str]) -> None:
            self._ids = _ids

        async def recover_fulfillment(self, fulfillment_id: str) -> bool:
            self._ids.append(fulfillment_id)
            return True

    workflow = workflow_module.ReplyFulfillmentWorkflow(
        repository=repository,
        proactive_reservation=resolution_spy,
        config_getter=config,
        recovery_senders_getter=lambda: recovery_senders or {},
        commitment_workflow=_Commitments(commitment_ids),
        alert_service=AlertSpy(called),
    )
    return workflow, called, commitment_ids


class CommitmentDownstreams:
    """送达后承诺下游能力替身：只记录调用以断言「受限时不得流入」。"""

    def __init__(self) -> None:
        self.confirmed: list[tuple[str, str, int]] = []
        self.favor: list[str] = []
        self.assistant: list[str] = []
        self.interaction: list[str] = []
        self.alerted: list[str] = []

    async def confirm(
        self, group_id: str, reservation_id: str, *, cooldown_seconds: int
    ) -> None:
        self.confirmed.append((group_id, reservation_id, cooldown_seconds))

    async def adjust_user_favorability(
        self, user_id: str, _delta: int, *, operation_id: str
    ) -> SimpleNamespace:
        self.favor.append(f"{user_id}:{operation_id}")
        return SimpleNamespace(before=0, after=1, delta=_delta)

    async def push_message_once(self, *_a: object, **_: object) -> bool:
        self.assistant.append("assistant")
        return True

    async def push_global_interaction_once(self, **_: object) -> bool:
        self.interaction.append("interaction")
        return True

    async def recover_alerts(self) -> None:
        self.alerted.append("alert")

    async def delete_chat_commit_evidence(self, fulfillment_id: str) -> None:
        self.alerted.append(f"evidence-{fulfillment_id}")

    async def delete_favorability_operation(self, operation_id: str) -> None:
        self.alerted.append(f"favor-op-{operation_id}")


class CommitmentRepo:
    """送达后承诺执行的父/子内存替身（真实父子表形状）。"""

    def __init__(self) -> None:
        self.parents: dict[str, dict[str, Any]] = {}
        self.children: dict[str, dict[str, dict[str, Any]]] = {}
        self.completed: set[str] = set()

    def _payloads(self) -> dict[str, dict[str, Any]]:
        return {
            "proactive_reply_confirmation": {
                "group_id": "group-1",
                "reservation_id": "reservation-1",
                "cooldown_seconds": 300,
            },
            "favorability_adjustment": {
                "user_id": "user-1",
                "delta": 1,
                "reason": "正常互动",
            },
            "assistant_reply_history": {
                "group_id": "group-1",
                "bot_nickname": "小鞠",
                "reply_content": "回复正文",
                "reply_timestamp": 2.0,
            },
            "interaction_history": {
                "user_id": "user-1",
                "display_name": "测试用户",
                "trigger_size": 20,
                "reply_timestamp": 2.0,
                "trigger_message_id": "message-1",
                "record": {"event": "发言", "result": "回复", "emotion": "平静"},
            },
        }

    def seed(
        self,
        fulfillment_id: str,
        *,
        delivery_state: str = "DELIVERED",
        group_id: str = "group-1",
        payloads: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        actual = payloads if payloads is not None else self._payloads()
        self.parents[fulfillment_id] = {
            "fulfillment_id": fulfillment_id,
            "delivery_state": delivery_state,
            "group_id": group_id,
            "completed": False,
            "lease_owner": None,
        }
        self.children[fulfillment_id] = {
            t: {
                "commitment_type": t,
                "payload": payload,
                "state": "PENDING",
                "attempt_count": 0,
                "completed": False,
            }
            for t, payload in actual.items()
        }

    # 下面方法：claim / load / mark / complete / cleanup
    async def claim_lease(
        self, fulfillment_id: str, *, owner_token: str, lease_seconds: int
    ) -> dict[str, Any] | None:
        del lease_seconds
        parent = self.parents[fulfillment_id]
        if parent["lease_owner"] is not None:
            return None
        parent["lease_owner"] = owner_token
        return {"fulfillment_id": fulfillment_id}

    async def load_claimed_commitments(
        self, fulfillment_id: str, *, owner_token: str
    ) -> list[dict[str, Any]] | None:
        parent = self.parents[fulfillment_id]
        if parent["lease_owner"] != owner_token:
            return None
        return [
            dict(child)
            for child in self.children[fulfillment_id].values()
            if child["state"] != "COMPLETED"
        ]

    async def mark_commitment_completed(
        self, fulfillment_id: str, *, commitment_type: str, owner_token: str
    ) -> bool:
        if self.parents[fulfillment_id]["lease_owner"] != owner_token:
            return False
        child = self.children[fulfillment_id][commitment_type]
        child["state"] = "COMPLETED"
        child["completed"] = True
        return True

    async def mark_commitment_failed(self, **_: object) -> str:
        return "FAILED"

    async def complete_fulfillment(
        self, fulfillment_id: str, *, owner_token: str
    ) -> bool:
        if self.parents[fulfillment_id]["lease_owner"] != owner_token:
            return False
        self.parents[fulfillment_id]["lease_owner"] = None
        self.parents[fulfillment_id]["completed"] = True
        self.completed.add(fulfillment_id)
        return True

    async def release_lease(
        self, fulfillment_id: str, *, owner_token: str
    ) -> bool:
        parent = self.parents[fulfillment_id]
        if parent["lease_owner"] != owner_token:
            return False
        parent["lease_owner"] = None
        return True

    async def renew_lease(
        self, fulfillment_id: str, *, owner_token: str, lease_seconds: int
    ) -> bool:
        del lease_seconds
        return self.parents[fulfillment_id]["lease_owner"] == owner_token

    async def claim_terminal_cleanup_candidates(
        self, *, owner_token: str, limit: int, lease_seconds: int, protection_days: int
    ) -> list[dict[str, Any]]:
        del owner_token, limit, lease_seconds, protection_days
        return [
            {"fulfillment_id": fid}
            for fid, parent in self.parents.items()
            if not parent["completed"]
        ]

    async def load_parent_attribution(
        self, fulfillment_id: str
    ) -> dict[str, Any] | None:
        parent = self.parents.get(fulfillment_id)
        if parent is None:
            return None
        return {
            "fulfillment_id": fulfillment_id,
            "group_id": parent.get("group_id"),
        }

    async def mark_idempotency_evidence_cleared(
        self, fulfillment_id: str, *, owner_token: str
    ) -> bool:
        return self.parents[fulfillment_id]["lease_owner"] == owner_token

    async def delete_terminal_tombstone(
        self, fulfillment_id: str, *, owner_token: str
    ) -> bool:
        del fulfillment_id, owner_token
        return True


def build_commitment_workflow(
    repository: CommitmentRepo,
    downstream: CommitmentDownstreams,
) -> tuple[Any, Any]:
    """装配真实 ``ReplyCommitmentWorkflow``，注入下游 Fake。"""
    cw_module = module("komari_bot.plugins.komari_chat.services.reply_commitment_workflow")
    workflow = cw_module.ReplyCommitmentWorkflow(
        repository=repository,
        redis=SimpleNamespace(
            delete_chat_commit_evidence=downstream.delete_chat_commit_evidence,
            push_message_once=downstream.push_message_once,
            push_global_interaction_once=downstream.push_global_interaction_once,
        ),
        proactive_reservation=downstream,
        user_data=downstream,
        config_getter=config,
    )
    return workflow, downstream
