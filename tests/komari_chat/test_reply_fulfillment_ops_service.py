"""回复履约受审计运维服务的窄边界验收测试。"""

from __future__ import annotations

import inspect
from copy import deepcopy
from importlib import import_module
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from nonebug import App


_COMMITMENT_TYPES = (
    "proactive_reply_confirmation",
    "favorability_adjustment",
    "assistant_reply_history",
    "interaction_history",
)


def _commitments(*, failed: str | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for commitment_type in _COMMITMENT_TYPES:
        state = "FAILED" if commitment_type == failed else "PENDING"
        rows.append(
            {
                "commitment_type": commitment_type,
                "state": state,
                "attempt_count": 3 if state == "FAILED" else 0,
                "next_retry_at": None,
                "last_error_code": "service_unavailable" if state == "FAILED" else None,
                "completed_at": None,
                "payload": {
                    "secret_canary": f"payload-{commitment_type}",
                    "reply_content": "绝不能出现在运维投影里的回复正文",
                },
            }
        )
    return rows


def _record(
    fulfillment_id: str,
    *,
    delivery_state: str,
    completed: bool = False,
    failed: str | None = None,
) -> dict[str, Any]:
    return {
        "fulfillment_id": fulfillment_id,
        "payload_hash": (fulfillment_id[-1] if fulfillment_id else "a") * 64,
        "request_trace_id": f"trace-{fulfillment_id}",
        "trigger_message_id": f"message-{fulfillment_id}",
        "trigger_user_id": "raw-user-canary",
        "group_id": "group-1",
        "bot_self_id": "bot-1",
        "adapter_name": "onebot.v11",
        "reply_target_message_id": "message-target-1",
        "reply_content": "待确认回复正文",
        "delivery_state": delivery_state,
        "prepared_at": "2026-08-11T00:00:00+00:00",
        "send_started_at": (
            "2026-08-11T00:00:01+00:00"
            if delivery_state != "NOT_STARTED"
            else None
        ),
        "delivered_at": (
            "2026-08-11T00:00:02+00:00"
            if delivery_state == "DELIVERED"
            else None
        ),
        "platform_message_id": (
            "platform-1" if delivery_state == "DELIVERED" else None
        ),
        "not_delivered_at": (
            "2026-08-11T00:00:02+00:00"
            if delivery_state == "NOT_DELIVERED"
            else None
        ),
        "completed_at": (
            "2026-08-11T00:00:03+00:00" if completed else None
        ),
        "lease_owner": "internal-owner-canary",
        "lease_expires_at": "2026-08-11T00:10:00+00:00",
        "commitments": _commitments(failed=failed),
    }


class _Repository:
    def __init__(self, module: Any) -> None:
        self.module = module
        self.records: dict[str, dict[str, Any]] = {}
        self.list_calls: list[dict[str, Any]] = []
        self.delivery_calls: list[tuple[str, str | None]] = []
        self.not_delivered_calls: list[str] = []
        self.resume_calls: list[tuple[str, str]] = []

    def seed(self, row: dict[str, Any]) -> None:
        self.records[str(row["fulfillment_id"])] = deepcopy(row)

    async def list_for_management(
        self,
        *,
        status: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        self.list_calls.append({"status": status, "limit": limit, "offset": offset})
        rows = list(self.records.values())
        if status is not None:
            rows = [
                row
                for row in rows
                if self.module.derive_reply_fulfillment_status(row) == status
            ]
        return deepcopy(rows[offset : offset + limit]), len(rows)

    async def get_for_management(
        self,
        fulfillment_id: str,
    ) -> dict[str, Any] | None:
        row = self.records.get(fulfillment_id)
        return deepcopy(row) if row is not None else None

    async def reconcile_delivered(
        self,
        fulfillment_id: str,
        *,
        platform_message_id: str | None,
    ) -> str:
        self.delivery_calls.append((fulfillment_id, platform_message_id))
        row = self.records.get(fulfillment_id)
        if row is None:
            return "not_found"
        state = row["delivery_state"]
        if state == "DELIVERED":
            existing = row["platform_message_id"]
            if existing is not None and platform_message_id not in {None, existing}:
                return "platform_message_conflict"
            if existing is None and platform_message_id is not None:
                row["platform_message_id"] = platform_message_id
            return "idempotent"
        if state != "PENDING_CONFIRMATION":
            return "state_conflict"
        row["delivery_state"] = "DELIVERED"
        row["delivered_at"] = "2026-08-11T00:00:02+00:00"
        row["platform_message_id"] = platform_message_id
        row["reply_content"] = None
        return "updated"

    async def reconcile_not_delivered(self, fulfillment_id: str) -> dict[str, Any]:
        self.not_delivered_calls.append(fulfillment_id)
        row = self.records.get(fulfillment_id)
        if row is None:
            return {"outcome": "not_found"}
        state = row["delivery_state"]
        if state == "NOT_DELIVERED":
            return {"outcome": "idempotent"}
        if state != "PENDING_CONFIRMATION":
            return {"outcome": "state_conflict"}
        proactive = next(
            item
            for item in row["commitments"]
            if item["commitment_type"] == "proactive_reply_confirmation"
        )
        proactive["payload"] = {
            "group_id": "group-1",
            "reservation_id": "reservation-1",
            "cooldown_seconds": 300,
        }
        result = {
            "outcome": "updated",
            "proactive_group_id": proactive["payload"]["group_id"],
            "proactive_reservation_id": proactive["payload"]["reservation_id"],
        }
        row["delivery_state"] = "NOT_DELIVERED"
        row["not_delivered_at"] = "2026-08-11T00:00:02+00:00"
        row["reply_content"] = None
        for item in row["commitments"]:
            item["payload"] = None
        return result

    async def resume_failed_commitment(
        self,
        fulfillment_id: str,
        *,
        commitment_type: str,
    ) -> str:
        self.resume_calls.append((fulfillment_id, commitment_type))
        row = self.records.get(fulfillment_id)
        if row is None:
            return "not_found"
        if row["delivery_state"] != "DELIVERED" or row["completed_at"] is not None:
            return "state_conflict"
        child = next(
            (
                item
                for item in row["commitments"]
                if item["commitment_type"] == commitment_type
            ),
            None,
        )
        if child is None:
            return "not_found"
        if child["state"] != "FAILED":
            return "state_conflict"
        child["state"] = "PENDING"
        child["attempt_count"] = 0
        child["next_retry_at"] = None
        child["last_error_code"] = None
        child["completed_at"] = None
        return "updated"


class _ProactiveReservation:
    def __init__(self) -> None:
        self.release_calls: list[tuple[str, str]] = []
        self.error: BaseException | None = None

    async def release(self, group_id: str, reservation_id: str) -> bool:
        self.release_calls.append((group_id, reservation_id))
        if self.error is not None:
            raise self.error
        return True


@pytest.fixture
def ops_module(app: App) -> Any:
    del app
    return import_module(
        "komari_bot.plugins.komari_chat.services.reply_fulfillment_ops"
    )


def _service(module: Any) -> tuple[Any, _Repository, _ProactiveReservation]:
    repository = _Repository(module)
    proactive = _ProactiveReservation()
    return (
        module.ReplyFulfillmentOpsService(repository, proactive),
        repository,
        proactive,
    )


@pytest.mark.asyncio
async def test_list_projects_derived_states_without_body_or_internal_payloads(
    ops_module: Any,
) -> None:
    service, repository, _proactive = _service(ops_module)
    repository.seed(_record("reply-a", delivery_state="NOT_STARTED"))
    repository.seed(_record("reply-b", delivery_state="PENDING_CONFIRMATION"))
    repository.seed(_record("reply-c", delivery_state="DELIVERED"))
    repository.seed(
        _record(
            "reply-d",
            delivery_state="DELIVERED",
            failed="favorability_adjustment",
        )
    )
    repository.seed(
        _record("reply-e", delivery_state="DELIVERED", completed=True)
    )
    repository.seed(_record("reply-f", delivery_state="NOT_DELIVERED"))

    result = await service.list_fulfillments(status=None, limit=20, offset=0)

    assert [item["status"] for item in result["items"]] == [
        "not_started",
        "pending_confirmation",
        "processing",
        "needs_disposition",
        "completed",
        "not_delivered",
    ]
    assert result["total"] == 6
    assert repository.list_calls == [{"status": None, "limit": 20, "offset": 0}]
    for item in result["items"]:
        serialized = repr(item)
        assert "reply_content" not in item
        assert "trigger_user_id" not in item
        assert "bot_self_id" not in item
        assert "adapter_name" not in item
        assert "lease_owner" not in item
        assert "lease_expires_at" not in item
        assert "payload" not in serialized
        assert "payload-" not in serialized
        assert item["reply_fingerprint"]
        assert all(
            set(commitment) == {
                "commitment_type",
                "state",
                "attempt_count",
                "next_retry_at",
                "last_error_code",
                "completed_at",
            }
            for commitment in item["commitments"]
        )


@pytest.mark.asyncio
async def test_list_preserves_repository_derived_status_without_internal_state(
    ops_module: Any,
) -> None:
    """安全 Repository 已推导的状态不应依赖内部 delivery_state 再计算。"""
    service, repository, _proactive = _service(ops_module)
    pending = _record("reply-safe-pending", delivery_state="PENDING_CONFIRMATION")
    pending.pop("delivery_state")
    pending["status"] = "pending_confirmation"
    repository.seed(pending)

    result = await service.list_fulfillments(status=None, limit=20, offset=0)

    assert result["items"][0]["status"] == "pending_confirmation"


@pytest.mark.asyncio
async def test_detail_only_exposes_body_for_pending_confirmation(
    ops_module: Any,
) -> None:
    service, repository, _proactive = _service(ops_module)
    repository.seed(_record("reply-pending", delivery_state="PENDING_CONFIRMATION"))
    delivered = _record(
        "reply-delivered",
        delivery_state="DELIVERED",
        failed="favorability_adjustment",
    )
    delivered["reply_content"] = "崩溃残留也不能泄漏"
    repository.seed(delivered)

    pending = await service.get_fulfillment("reply-pending")
    completed = await service.get_fulfillment("reply-delivered")

    assert pending is not None
    assert pending["reply_content"] == "待确认回复正文"
    assert pending["reply_target_message_id"] == "message-target-1"
    assert completed is not None
    assert completed["reply_content"] is None
    assert "崩溃残留也不能泄漏" not in repr(completed)
    assert "secret_canary" not in repr(pending)
    assert await service.get_fulfillment("reply-missing") is None


@pytest.mark.asyncio
async def test_confirm_delivered_is_idempotent_and_rejects_conflicting_evidence(
    ops_module: Any,
) -> None:
    service, repository, _proactive = _service(ops_module)
    repository.seed(_record("reply-delivery", delivery_state="PENDING_CONFIRMATION"))

    first = await service.confirm_delivered(
        "reply-delivery",
        platform_message_id="platform-1",
    )
    replay = await service.confirm_delivered(
        "reply-delivery",
        platform_message_id="platform-1",
    )

    assert first["idempotent_replay"] is False
    assert replay["idempotent_replay"] is True
    assert repository.records["reply-delivery"]["reply_content"] is None
    with pytest.raises(ops_module.ReplyFulfillmentOpsConflictError):
        await service.confirm_delivered(
            "reply-delivery",
            platform_message_id="platform-conflict",
        )
    assert repository.records["reply-delivery"]["platform_message_id"] == "platform-1"


@pytest.mark.asyncio
async def test_confirm_delivered_without_platform_id_remains_idempotent(
    ops_module: Any,
) -> None:
    service, repository, _proactive = _service(ops_module)
    repository.seed(_record("reply-no-platform", delivery_state="PENDING_CONFIRMATION"))

    first = await service.confirm_delivered(
        "reply-no-platform",
        platform_message_id=None,
    )
    replay = await service.confirm_delivered(
        "reply-no-platform",
        platform_message_id=None,
    )

    assert first["idempotent_replay"] is False
    assert replay["idempotent_replay"] is True


@pytest.mark.asyncio
async def test_confirm_delivered_replay_persists_late_platform_evidence(
    ops_module: Any,
) -> None:
    """首次无平台 ID 后补同一送达证据时，不能只回显而不持久化。"""
    service, repository, _proactive = _service(ops_module)
    repository.seed(_record("reply-late-platform", delivery_state="PENDING_CONFIRMATION"))

    await service.confirm_delivered(
        "reply-late-platform",
        platform_message_id=None,
    )
    replay = await service.confirm_delivered(
        "reply-late-platform",
        platform_message_id="platform-late-1",
    )

    assert replay["idempotent_replay"] is True
    assert (
        repository.records["reply-late-platform"]["platform_message_id"]
        == "platform-late-1"
    )


@pytest.mark.asyncio
async def test_confirm_not_delivered_releases_reservation_without_running_commitments(
    ops_module: Any,
) -> None:
    service, repository, proactive = _service(ops_module)
    repository.seed(_record("reply-rejected", delivery_state="PENDING_CONFIRMATION"))

    first = await service.confirm_not_delivered("reply-rejected")
    replay = await service.confirm_not_delivered("reply-rejected")

    assert first == {"idempotent_replay": False, "reservation_released": True}
    assert replay == {"idempotent_replay": True, "reservation_released": False}
    assert proactive.release_calls == [("group-1", "reservation-1")]
    stored = repository.records["reply-rejected"]
    assert stored["delivery_state"] == "NOT_DELIVERED"
    assert all(item["state"] == "PENDING" for item in stored["commitments"])
    assert all(item["payload"] is None for item in stored["commitments"])


@pytest.mark.asyncio
async def test_not_delivered_fact_survives_reservation_release_failure(
    ops_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repository, proactive = _service(ops_module)
    repository.seed(_record("reply-release-failed", delivery_state="PENDING_CONFIRMATION"))
    proactive.error = ConnectionError("raw-release-error-canary")
    warnings: list[str] = []

    class _Logger:
        @staticmethod
        def warning(message: str, *_args: object) -> None:
            warnings.append(message)

    monkeypatch.setattr(ops_module, "logger", _Logger(), raising=False)

    result = await service.confirm_not_delivered("reply-release-failed")

    assert result == {"idempotent_replay": False, "reservation_released": False}
    assert repository.records["reply-release-failed"]["delivery_state"] == "NOT_DELIVERED"
    assert warnings == ["[KomariChat] 履约对账释放主动回复预占失败，等待 TTL 回收"]
    assert "raw-release-error-canary" not in repr(warnings)
    assert "reply-release-failed" not in repr(warnings)


@pytest.mark.asyncio
async def test_resume_only_failed_commitment_without_modifying_frozen_payload(
    ops_module: Any,
) -> None:
    service, repository, _proactive = _service(ops_module)
    repository.seed(
        _record(
            "reply-needs-disposition",
            delivery_state="DELIVERED",
            failed="favorability_adjustment",
        )
    )
    child = repository.records["reply-needs-disposition"]["commitments"][1]
    frozen_payload = deepcopy(child["payload"])

    result = await service.resume_commitment(
        "reply-needs-disposition",
        commitment_type="favorability_adjustment",
    )

    assert result["commitment_type"] == "favorability_adjustment"
    assert result["state"] == "PENDING"
    assert child["state"] == "PENDING"
    assert child["attempt_count"] == 0
    assert child["last_error_code"] is None
    assert child["payload"] == frozen_payload
    with pytest.raises(ops_module.ReplyFulfillmentOpsConflictError):
        await service.resume_commitment(
            "reply-needs-disposition",
            commitment_type="favorability_adjustment",
        )
    with pytest.raises(ops_module.ReplyFulfillmentOpsValidationError):
        await service.resume_commitment(
            "reply-needs-disposition",
            commitment_type="dynamic_commitment",
        )


@pytest.mark.asyncio
async def test_resume_keeps_needs_disposition_when_failed_sibling_remains(
    ops_module: Any,
) -> None:
    """续跑单项后若仍有失败兄弟项，响应状态不得伪报 processing。"""
    service, repository, _proactive = _service(ops_module)
    row = _record(
        "reply-multiple-failed",
        delivery_state="DELIVERED",
        failed="favorability_adjustment",
    )
    row["commitments"][2]["state"] = "FAILED"
    row["commitments"][2]["attempt_count"] = 2
    row["commitments"][2]["last_error_code"] = "service_unavailable"
    repository.seed(row)

    result = await service.resume_commitment(
        "reply-multiple-failed",
        commitment_type="favorability_adjustment",
    )

    assert result["status"] == "needs_disposition"


def test_ops_public_methods_do_not_accept_internal_payload_or_owner_tokens(
    ops_module: Any,
) -> None:
    forbidden = {"payload", "owner_token", "repository", "lease_owner"}
    for method_name in (
        "list_fulfillments",
        "get_fulfillment",
        "confirm_delivered",
        "confirm_not_delivered",
        "resume_commitment",
    ):
        method = getattr(ops_module.ReplyFulfillmentOpsService, method_name)
        assert forbidden.isdisjoint(inspect.signature(method).parameters)
