"""TSK-195 delivered-memory commitment and output-protocol retirement.

The tests drive the real production seams: the fulfillment domain/value
objects, ``ReplyFulfillmentWorkflow.fulfill``, the generation wrapper in
``MessageHandler`` and ``ReplyCommitmentWorkflow.recover_fulfillment``.
The Hindsight SDK boundary is replaced by a task-session double so the
test observes exactly which serialized request would reach the shared
caller, and when.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from importlib import import_module
from types import SimpleNamespace
from typing import Any

import pytest

from komari_bot.context.redis_store import MessageSchema
from komari_bot.plugins.komari_chat.reply_fulfillment_domain import (
    COMMITMENT_TYPES,
)
from tests.komari_chat.hindsight_recall_support import (
    FakeHindsightMemory,
    fake_recall_session,
)

_TARGET_FINGERPRINT = "a" * 64
_FULFILLMENT_ID = "reply-" + "b" * 64


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _reply_document(
    *,
    fulfillment_id: str = _FULFILLMENT_ID,
    reply_content: str = "回复正文",
    group_id: str = "group-1",
    bot_self_id: str = "bot-1",
    adapter_name: str = "onebot.v11",
    trigger_message_id: str = "message-1",
    trigger_user_id: str = "user-1",
    reply_target_message_id: str = "message-1",
    generated_at: float = 2.0,
) -> str:
    """The frozen human-readable reply document carried by the commitment."""
    del fulfillment_id
    return _canonical(
        {
            "adapter_name": adapter_name,
            "bot_self_id": bot_self_id,
            "generated_at": "1970-01-01T00:00:02Z"
            if generated_at == 2.0
            else datetime.fromtimestamp(generated_at, UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "group_id": group_id,
            "reply_content": reply_content,
            "reply_target_message_id": reply_target_message_id,
            "trigger_message_id": trigger_message_id,
            "trigger_user_id": trigger_user_id,
        }
    )


def _serialized_retain_request(
    *,
    fulfillment_id: str = _FULFILLMENT_ID,
    target_fingerprint: str = _TARGET_FINGERPRINT,
    reply_content: str = "回复正文",
    group_id: str = "group-1",
    bot_self_id: str = "bot-1",
    adapter_name: str = "onebot.v11",
    trigger_message_id: str = "message-1",
    trigger_user_id: str = "user-1",
    reply_target_message_id: str = "message-1",
    generated_at: float = 2.0,
    bot_nickname: str = "小鞠",
) -> str:
    retain_module = import_module(
        "komari_bot.plugins.komari_memory.services.hindsight_retain"
    )
    request = retain_module.HindsightRetainRequest.create(
        target_fingerprint=target_fingerprint,
        source_id=f"reply:{fulfillment_id}",
        content=_reply_document(
            fulfillment_id=fulfillment_id,
            reply_content=reply_content,
            group_id=group_id,
            bot_self_id=bot_self_id,
            adapter_name=adapter_name,
            trigger_message_id=trigger_message_id,
            trigger_user_id=trigger_user_id,
            reply_target_message_id=reply_target_message_id,
            generated_at=generated_at,
        ),
        timestamp=datetime.fromtimestamp(generated_at, UTC),
        metadata={"bot_nickname": bot_nickname},
    )
    return _canonical(request.to_json())


def test_domain_registers_hindsight_memory_and_rejects_legacy_active_payload() -> None:
    """Active commitments are the three preserved effects plus Hindsight."""
    domain = import_module("komari_bot.plugins.komari_chat.reply_fulfillment_domain")

    assert COMMITMENT_TYPES == (
        "proactive_reply_confirmation",
        "favorability_adjustment",
        "assistant_reply_history",
        "hindsight_memory",
    )
    payload = domain.HindsightMemoryPayload(request=_serialized_retain_request())
    assert payload.to_json() == {"request": _serialized_retain_request()}

    with pytest.raises((TypeError, ValueError)):
        domain.HindsightMemoryPayload(request="[]")
    with pytest.raises(ValueError):
        domain.HindsightMemoryPayload(
            request=json.dumps({"version": 1}, ensure_ascii=False)
        )
    with pytest.raises(ValueError):
        domain.ReplyCommitmentInput(
            commitment_type="interaction_history",
            payload=SimpleNamespace(to_json=dict),  # type: ignore[arg-type]
        )


class _DraftStore:
    """Fulfillment repository double that keeps the first frozen draft."""

    def __init__(self) -> None:
        self.records: dict[str, Any] = {}

    async def has_fulfillment(self, fulfillment_id: str) -> bool:
        return fulfillment_id in self.records

    async def prepare(self, draft: Any) -> bool:
        if draft.fulfillment_id in self.records:
            return False
        self.records[draft.fulfillment_id] = draft
        return True

    async def mark_send_started(self, fulfillment_id: str) -> bool:
        return fulfillment_id in self.records


class _Handoff:
    group_id = "group-1"
    reservation_id = "reservation-1"
    cooldown_seconds = 300

    async def release(self) -> bool:
        return True


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        proactive_cooldown=300,
        global_interaction_enabled=False,
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
    *,
    reply_content: str = "回复正文",
    bot_nickname: str = "小鞠",
    bot_self_id: str = "bot-1",
    adapter_name: str = "onebot.v11",
    target_fingerprint: str = _TARGET_FINGERPRINT,
    with_reservation: bool = True,
    reply_timestamp: float = 2.0,
) -> Any:
    handler_module = import_module(
        "komari_bot.plugins.komari_chat.handlers.message_handler"
    )
    fulfillment_id = _FULFILLMENT_ID
    message = MessageSchema(
        user_id="user-1",
        user_nickname="测试用户",
        group_id="group-1",
        content="用户正文",
        timestamp=1.0,
        message_id="message-1",
    )
    return handler_module.PendingReply(
        reply=reply_content,
        reply_to_message_id="message-1",
        message=message,
        reply_result=import_module("komari_bot.plugins.komari_chat.services.chat_result").ReplyResult(
            content=reply_content,
            favorability_delta=1,
            favorability_reason="正常互动",
            hindsight_target_fingerprint=target_fingerprint,
        ),
        force_reply=False,
        bot_nickname=bot_nickname,
        bot_self_id=bot_self_id,
        adapter_name=adapter_name,
        reason="optional",
        fulfillment_id=fulfillment_id,
        request_trace_id="chat-message-1",
        reply_timestamp=reply_timestamp,
        proactive_reservation_id="reservation-1" if with_reservation else None,
        proactive_handoff=_Handoff() if with_reservation else None,
        hindsight_target_fingerprint=target_fingerprint,
    )


def _fulfillment_workflow(module: Any, store: _DraftStore) -> Any:
    return module.ReplyFulfillmentWorkflow(
        repository=store,
        proactive_reservation=SimpleNamespace(),
        config_getter=_config,
        recovery_senders_getter=dict,
        commitment_workflow=SimpleNamespace(),
        alert_service=SimpleNamespace(),
    )


async def test_fulfill_freezes_one_serialized_hindsight_request_before_send() -> None:
    """The DELIVERED executor gets a frozen request, never a rebuilt one."""
    workflow_module = import_module(
        "komari_bot.plugins.komari_chat.services.reply_fulfillment_workflow"
    )
    store = _DraftStore()
    workflow = _fulfillment_workflow(workflow_module, store)
    pending = _pending_reply()

    async def _cancel(_pending_reply: object) -> object:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await workflow.fulfill(pending, send_reply=_cancel)

    draft = store.records[pending.fulfillment_id]
    assert [item.commitment_type for item in draft.commitments] == [
        "proactive_reply_confirmation",
        "favorability_adjustment",
        "assistant_reply_history",
        "hindsight_memory",
    ]
    memory_payload = next(
        item for item in draft.commitments if item.commitment_type == "hindsight_memory"
    )
    assert memory_payload.to_json() == {
        "request": _serialized_retain_request(fulfillment_id=pending.fulfillment_id)
    }
    assert draft.reply_content == "回复正文"


async def test_fulfill_requires_memory_even_when_legacy_interaction_flag_is_off() -> (
    None
):
    """Hindsight is always applicable; the retired interaction gate is gone."""
    workflow_module = import_module(
        "komari_bot.plugins.komari_chat.services.reply_fulfillment_workflow"
    )
    store = _DraftStore()
    workflow = _fulfillment_workflow(workflow_module, store)
    pending = _pending_reply(with_reservation=False)

    async def _cancel(_pending_reply: object) -> object:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await workflow.fulfill(pending, send_reply=_cancel)

    draft = store.records[pending.fulfillment_id]
    assert [item.commitment_type for item in draft.commitments] == [
        "favorability_adjustment",
        "assistant_reply_history",
        "hindsight_memory",
    ]


class _CommitmentRepository:
    """In-memory parent/child fulfillment store for executor tests."""

    def __init__(self) -> None:
        self.parents: dict[str, dict[str, Any]] = {}
        self.children: dict[str, dict[str, dict[str, Any]]] = {}
        self.failure_calls: list[tuple[str, str, str]] = []

    def seed(
        self,
        fulfillment_id: str,
        *,
        group_id: str = "group-1",
        include_proactive: bool = False,
        request: str | None = None,
        delivery_state: str = "DELIVERED",
    ) -> None:
        payloads: dict[str, dict[str, Any]] = {
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
            "hindsight_memory": {
                "request": request
                or _serialized_retain_request(fulfillment_id=fulfillment_id)
            },
        }
        if include_proactive:
            payloads = {
                "proactive_reply_confirmation": {
                    "group_id": group_id,
                    "reservation_id": "reservation-1",
                    "cooldown_seconds": 300,
                },
                **payloads,
            }
        self.parents[fulfillment_id] = {
            "fulfillment_id": fulfillment_id,
            "delivery_state": delivery_state,
            "group_id": group_id,
            "completed": False,
            "lease_owner": None,
        }
        self.children[fulfillment_id] = {
            commitment_type: {
                "commitment_type": commitment_type,
                "payload": payload,
                "state": "PENDING",
                "attempt_count": 0,
                "last_error_code": None,
            }
            for commitment_type, payload in payloads.items()
        }

    def snapshot(self, fulfillment_id: str, commitment_type: str) -> Any:
        return SimpleNamespace(**self.children[fulfillment_id][commitment_type])

    async def claim_lease(
        self,
        fulfillment_id: str,
        *,
        owner_token: str,
        lease_seconds: int,
    ) -> dict[str, Any] | None:
        del lease_seconds
        parent = self.parents[fulfillment_id]
        if parent["lease_owner"] is not None:
            return None
        parent["lease_owner"] = owner_token
        return {"fulfillment_id": fulfillment_id, "group_id": parent["group_id"]}

    async def claim_pending(
        self,
        *,
        owner_token: str,
        limit: int,
        lease_seconds: int,
    ) -> list[dict[str, Any]]:
        del lease_seconds
        claimed: list[dict[str, Any]] = []
        for fulfillment_id, parent in self.parents.items():
            if len(claimed) >= limit:
                break
            if parent["delivery_state"] != "DELIVERED" or parent["completed"]:
                continue
            if parent["lease_owner"] is not None:
                continue
            parent["lease_owner"] = owner_token
            claimed.append(
                {"fulfillment_id": fulfillment_id, "group_id": parent["group_id"]}
            )
        return claimed

    async def load_claimed_commitments(
        self,
        fulfillment_id: str,
        *,
        owner_token: str,
    ) -> list[dict[str, Any]] | None:
        if self.parents[fulfillment_id]["lease_owner"] != owner_token:
            return None
        return [
            dict(child)
            for child in self.children[fulfillment_id].values()
            if child["state"] == "PENDING"
        ]

    async def load_parent_attribution(
        self,
        fulfillment_id: str,
    ) -> dict[str, Any] | None:
        parent = self.parents.get(fulfillment_id)
        if parent is None:
            return None
        return {"fulfillment_id": fulfillment_id, "group_id": parent["group_id"]}

    async def mark_commitment_completed(
        self,
        fulfillment_id: str,
        *,
        commitment_type: str,
        owner_token: str,
    ) -> bool:
        if self.parents[fulfillment_id]["lease_owner"] != owner_token:
            return False
        child = self.children[fulfillment_id][commitment_type]
        child.update(state="COMPLETED", last_error_code=None, payload=None)
        return True

    async def mark_commitment_failed(
        self,
        fulfillment_id: str,
        *,
        commitment_type: str,
        owner_token: str,
        error_code: str,
        max_attempts: int,
        retry_base_seconds: int,
        retry_max_seconds: int,
    ) -> str | None:
        del retry_base_seconds, retry_max_seconds
        if self.parents[fulfillment_id]["lease_owner"] != owner_token:
            return None
        child = self.children[fulfillment_id][commitment_type]
        attempts = int(child["attempt_count"]) + 1
        state = "FAILED" if attempts >= max_attempts else "RETRY_WAIT"
        child.update(state=state, attempt_count=attempts, last_error_code=error_code)
        self.failure_calls.append((fulfillment_id, commitment_type, error_code))
        return state

    async def complete_fulfillment(
        self,
        fulfillment_id: str,
        *,
        owner_token: str,
    ) -> bool:
        parent = self.parents[fulfillment_id]
        if parent["lease_owner"] != owner_token:
            return False
        if not all(
            child["state"] == "COMPLETED"
            for child in self.children[fulfillment_id].values()
        ):
            return False
        parent["completed"] = True
        parent["lease_owner"] = None
        return True

    async def renew_lease(
        self,
        fulfillment_id: str,
        *,
        owner_token: str,
        lease_seconds: int,
    ) -> bool:
        del lease_seconds
        return self.parents[fulfillment_id]["lease_owner"] == owner_token

    async def release_lease(
        self,
        fulfillment_id: str,
        *,
        owner_token: str,
    ) -> bool:
        parent = self.parents[fulfillment_id]
        if parent["lease_owner"] != owner_token:
            return False
        parent["lease_owner"] = None
        return True


class _RetainSession:
    def __init__(self, service: "_HindsightService") -> None:
        self.service = service
        self.target_fingerprint = service.target_fingerprint

    async def retain(
        self,
        request: Any,
        *,
        submission_guard: Any,
    ) -> None:
        self.service.retain_calls.append(request)
        await asyncio.sleep(0)
        if not submission_guard():
            raise self.service.error_module.HindsightUnavailableError(
                "submission_not_admitted"
            )
        if self.service.retain_error is not None:
            raise self.service.retain_error


class _SessionContext:
    def __init__(
        self,
        service: "_HindsightService",
        expected_target_fingerprint: str | None,
    ) -> None:
        self.service = service
        self.expected_target_fingerprint = expected_target_fingerprint

    async def __aenter__(self) -> _RetainSession:
        self.service.open_calls.append(self.expected_target_fingerprint)
        if self.service.open_error is not None:
            raise self.service.open_error
        if (
            self.expected_target_fingerprint is not None
            and self.expected_target_fingerprint != self.service.target_fingerprint
        ):
            raise self.service.error_module.HindsightUnavailableError("target_changed")
        session = _RetainSession(self.service)
        self.service.sessions.append(session)
        return session

    async def __aexit__(
        self,
        _exc_type: object,
        _exc: object,
        _traceback: object,
    ) -> None:
        return None


class _HindsightService:
    """Task-session double at the accepted shared caller boundary."""

    def __init__(self, *, target_fingerprint: str = _TARGET_FINGERPRINT) -> None:
        self.error_module = import_module(
            "komari_bot.plugins.komari_memory.services.hindsight_service"
        )
        self.target_fingerprint = target_fingerprint
        self.open_calls: list[str | None] = []
        self.sessions: list[_RetainSession] = []
        self.retain_calls: list[Any] = []
        self.open_error: BaseException | None = None
        self.retain_error: BaseException | None = None

    def open_session(
        self,
        *,
        expected_target_fingerprint: str | None = None,
    ) -> _SessionContext:
        return _SessionContext(self, expected_target_fingerprint)


def _commitment_workflow(
    module: Any,
    repository: _CommitmentRepository,
    memory: _HindsightService | None,
) -> Any:
    return module.ReplyCommitmentWorkflow(
        repository=repository,
        redis=SimpleNamespace(
            push_message_once=lambda *_a, **_k: asyncio.sleep(0, result=True),
        ),
        proactive_reservation=SimpleNamespace(),
        user_data=SimpleNamespace(
            adjust_user_favorability=lambda *_a, **_k: asyncio.sleep(0),
        ),
        config_getter=_config,
        hindsight_memory=memory,
    )


async def test_delivered_recovery_submits_exactly_one_frozen_retain_request() -> None:
    module = import_module(
        "komari_bot.plugins.komari_chat.services.reply_commitment_workflow"
    )
    repository = _CommitmentRepository()
    repository.seed(_FULFILLMENT_ID)
    memory = _HindsightService()
    workflow = _commitment_workflow(module, repository, memory)

    assert await workflow.recover_fulfillment(_FULFILLMENT_ID) is True

    assert memory.open_calls == [_TARGET_FINGERPRINT]
    assert len(memory.retain_calls) == 1
    request = memory.retain_calls[0]
    assert request.source_id == f"reply:{_FULFILLMENT_ID}"
    assert request.target_fingerprint == _TARGET_FINGERPRINT
    assert request.to_json() == json.loads(_serialized_retain_request())
    assert repository.snapshot(_FULFILLMENT_ID, "hindsight_memory").state == "COMPLETED"
    assert repository.parents[_FULFILLMENT_ID]["completed"] is True


async def test_document_conflict_is_permanent_idempotency_conflict_without_overwrite() -> (
    None
):
    module = import_module(
        "komari_bot.plugins.komari_chat.services.reply_commitment_workflow"
    )
    repository = _CommitmentRepository()
    repository.seed(_FULFILLMENT_ID)
    memory = _HindsightService()
    memory.retain_error = memory.error_module.HindsightUnavailableError(
        "document_conflict"
    )
    workflow = _commitment_workflow(module, repository, memory)

    assert await workflow.recover_fulfillment(_FULFILLMENT_ID) is False
    assert len(memory.retain_calls) == 1
    child = repository.snapshot(_FULFILLMENT_ID, "hindsight_memory")
    assert child.state == "FAILED"
    assert child.attempt_count == 1
    assert child.last_error_code == "idempotency_conflict"
    assert repository.parents[_FULFILLMENT_ID]["completed"] is False
    assert repository.parents[_FULFILLMENT_ID]["delivery_state"] == "DELIVERED"


async def test_memory_transport_errors_keep_bounded_code_and_retry_same_request() -> (
    None
):
    module = import_module(
        "komari_bot.plugins.komari_chat.services.reply_commitment_workflow"
    )
    repository = _CommitmentRepository()
    repository.seed(_FULFILLMENT_ID)
    memory = _HindsightService()
    memory.retain_error = memory.error_module.HindsightUnavailableError(
        "request_timeout"
    )
    workflow = _commitment_workflow(module, repository, memory)

    assert await workflow.recover_fulfillment(_FULFILLMENT_ID) is False
    child = repository.snapshot(_FULFILLMENT_ID, "hindsight_memory")
    assert child.state == "RETRY_WAIT"
    assert child.attempt_count == 1
    assert child.last_error_code == "request_timeout"
    assert repository.parents[_FULFILLMENT_ID]["completed"] is False

    memory.retain_error = None
    repository.children[_FULFILLMENT_ID]["hindsight_memory"]["state"] = "PENDING"
    assert await workflow.recover_fulfillment(_FULFILLMENT_ID) is True
    assert len(memory.retain_calls) == 2
    assert memory.retain_calls[0] == memory.retain_calls[1]
    assert memory.open_calls == [_TARGET_FINGERPRINT, _TARGET_FINGERPRINT]
    assert repository.parents[_FULFILLMENT_ID]["completed"] is True


async def test_changed_target_is_refused_before_any_retain_write() -> None:
    module = import_module(
        "komari_bot.plugins.komari_chat.services.reply_commitment_workflow"
    )
    repository = _CommitmentRepository()
    repository.seed(_FULFILLMENT_ID)
    memory = _HindsightService(target_fingerprint="c" * 64)
    workflow = _commitment_workflow(module, repository, memory)

    assert await workflow.recover_fulfillment(_FULFILLMENT_ID) is False
    assert memory.retain_calls == []
    child = repository.snapshot(_FULFILLMENT_ID, "hindsight_memory")
    assert child.state == "RETRY_WAIT"
    assert child.last_error_code == "target_changed"
    assert repository.parents[_FULFILLMENT_ID]["completed"] is False


async def test_rotated_credential_on_same_target_is_allowed() -> None:
    module = import_module(
        "komari_bot.plugins.komari_chat.services.reply_commitment_workflow"
    )
    repository = _CommitmentRepository()
    repository.seed(_FULFILLMENT_ID)
    memory = _HindsightService()
    workflow = _commitment_workflow(module, repository, memory)

    assert await workflow.recover_fulfillment(_FULFILLMENT_ID) is True
    # Only the endpoint/bank fingerprint crossed the boundary; credentials
    # are resolved inside the caller, so a rotation keeps the same identity.
    assert memory.open_calls == [_TARGET_FINGERPRINT]
    assert memory.retain_calls[0].target_fingerprint == _TARGET_FINGERPRINT


async def test_final_guard_denial_after_preflight_holds_without_retry_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = import_module(
        "komari_bot.plugins.komari_chat.services.reply_commitment_workflow"
    )
    repository = _CommitmentRepository()
    repository.seed(_FULFILLMENT_ID)
    memory = _HindsightService()
    workflow = _commitment_workflow(module, repository, memory)
    decisions = iter((True, True, True, False))
    monkeypatch.setattr(
        module,
        "reply_fulfillment_finalization_granted",
        lambda **_kwargs: next(decisions),
    )

    assert await workflow.recover_fulfillment(_FULFILLMENT_ID) is False

    child = repository.snapshot(_FULFILLMENT_ID, "hindsight_memory")
    assert child.state == "PENDING"
    assert child.attempt_count == 0
    assert child.last_error_code is None
    assert repository.failure_calls == []
    assert repository.parents[_FULFILLMENT_ID]["lease_owner"] is None
    assert repository.parents[_FULFILLMENT_ID]["completed"] is False


async def test_invalid_persisted_request_is_permanent_before_any_sdk_io() -> None:
    module = import_module(
        "komari_bot.plugins.komari_chat.services.reply_commitment_workflow"
    )
    repository = _CommitmentRepository()
    repository.seed(
        _FULFILLMENT_ID,
        request=json.dumps({"version": 1}, ensure_ascii=False),
    )
    memory = _HindsightService()
    workflow = _commitment_workflow(module, repository, memory)

    assert await workflow.recover_fulfillment(_FULFILLMENT_ID) is False
    assert memory.open_calls == []
    assert memory.retain_calls == []
    child = repository.snapshot(_FULFILLMENT_ID, "hindsight_memory")
    assert child.state == "FAILED"
    assert child.attempt_count == 1
    assert child.last_error_code == "invalid_payload"


async def test_memory_failure_leaves_independent_effects_completed_and_unsent_again() -> (
    None
):
    module = import_module(
        "komari_bot.plugins.komari_chat.services.reply_commitment_workflow"
    )
    repository = _CommitmentRepository()
    repository.seed(_FULFILLMENT_ID)
    memory = _HindsightService()
    memory.retain_error = memory.error_module.HindsightUnavailableError(
        "bank_unavailable"
    )
    workflow = _commitment_workflow(module, repository, memory)

    assert await workflow.recover_fulfillment(_FULFILLMENT_ID) is False
    assert (
        repository.snapshot(_FULFILLMENT_ID, "favorability_adjustment").state
        == "COMPLETED"
    )
    assert (
        repository.snapshot(_FULFILLMENT_ID, "assistant_reply_history").state
        == "COMPLETED"
    )
    memory_child = repository.snapshot(_FULFILLMENT_ID, "hindsight_memory")
    assert memory_child.state == "RETRY_WAIT"
    assert memory_child.last_error_code == "bank_unavailable"
    assert repository.parents[_FULFILLMENT_ID]["completed"] is False
    # Recovery never re-runs the platform send path; this workflow has no
    # sender dependency at all, so the only external call is the retain.
    assert len(memory.retain_calls) == 1


async def test_missing_hindsight_service_fails_safe_without_parent_loss() -> None:
    module = import_module(
        "komari_bot.plugins.komari_chat.services.reply_commitment_workflow"
    )
    repository = _CommitmentRepository()
    repository.seed(_FULFILLMENT_ID)
    workflow = _commitment_workflow(module, repository, None)

    assert await workflow.recover_fulfillment(_FULFILLMENT_ID) is False
    child = repository.snapshot(_FULFILLMENT_ID, "hindsight_memory")
    assert child.state == "RETRY_WAIT"
    assert child.last_error_code == "configuration_unavailable"
    assert repository.parents[_FULFILLMENT_ID]["delivery_state"] == "DELIVERED"
    assert repository.parents[_FULFILLMENT_ID]["completed"] is False


def test_final_response_accepts_content_only_and_never_takes_model_fingerprint() -> (
    None
):
    from komari_bot.plugins.komari_chat.services.chat_candidate import ChatCandidate

    candidate = ChatCandidate(max_favorability_delta=3)
    assert not candidate.submit_final_response({}).accepted
    assert not candidate.submit_final_response({"content": "   "}).accepted
    assert not candidate.submit_final_response({
        "content": "你好", "hindsight_target_fingerprint": "f" * 64,
    }).accepted
    assert not candidate.submit_final_response({
        "content": "你好", "interaction_history": {"event": "发言"},
    }).accepted
    assert candidate.submit_final_response({"content": "  你好  "}).accepted
    receipt = candidate.submit_favorability({"delta": 0, "reason": "无变化"})
    assert receipt.result == {
        "content": "你好", "favorability_delta": 0, "favorability_reason": "无变化",
    }
    assert receipt.result is not None
    assert "hindsight_target_fingerprint" not in receipt.result


async def test_generation_wrapper_annotates_the_actual_session_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler_module = import_module(
        "komari_bot.plugins.komari_chat.handlers.message_handler"
    )
    memory = import_module(
        "tests.komari_chat.hindsight_recall_support"
    ).FakeHindsightMemory()
    handler = handler_module.MessageHandler.__new__(handler_module.MessageHandler)
    handler.hindsight_memory = memory

    async def _generated(_self: object, **_kwargs: object) -> object:
        return import_module("komari_bot.plugins.komari_chat.services.chat_result").ReplyResult(content="收到啦")

    monkeypatch.setattr(
        handler_module.MessageHandler,
        "_generate_reply_with_session",
        _generated,
    )
    monkeypatch.setattr(
        handler_module,
        "effect_business_admitted",
        lambda **_kwargs: True,
    )

    result = await handler._generate_reply_core(
        message=MessageSchema(
            user_id="user-1",
            user_nickname="测试",
            group_id="group-1",
            content="你好",
            timestamp=1.0,
            message_id="message-1",
        ),
        recent_messages=[],
        image_urls=None,
        reply_context=None,
        reply_context_requested=False,
        reply_context_refetched=False,
        request_trace_id="chat-message-1",
    )

    assert result.hindsight_target_fingerprint == memory.sessions[0].target_fingerprint
    assert result.hindsight_target_fingerprint == "a" * 64


def _handler_config() -> SimpleNamespace:
    return SimpleNamespace(
        proactive_enabled=False,
        context_messages_limit=10,
        context_max_utf8_bytes=24_000,
        context_max_estimated_tokens=6_000,
        bot_nickname="小鞠",
        face_reaction_enabled=False,
        face_reaction_id="76",
        image_understanding_mode="native",
        agent_max_rounds=10,
        agent_max_tool_calls_per_round=4,
        agent_max_total_tool_calls=20,
        agent_tool_call_mode="required",
        vision_image_download_max_count=4,
        vision_image_download_max_bytes=8 * 1024 * 1024,
        vision_image_download_total_max_bytes=20 * 1024 * 1024,
        vision_image_download_max_pixels=40_000_000,
        vision_image_download_concurrency=2,
        vision_image_download_connect_timeout_seconds=5.0,
        vision_image_download_read_timeout_seconds=30.0,
        vision_image_download_total_timeout_seconds=45.0,
    )


class _EmptyRedis:
    async def get_buffer(self, _group_id: str, limit: int = 100) -> list[object]:
        del limit
        return []


def _attempt_handler(
    monkeypatch: pytest.MonkeyPatch,
    handler_module: Any,
    *,
    generated: Any,
) -> Any:
    handler = handler_module.MessageHandler.__new__(handler_module.MessageHandler)
    handler.redis = _EmptyRedis()
    handler.hindsight_memory = FakeHindsightMemory()
    handler.proactive_reservation = SimpleNamespace()

    async def _generate(_self: object, **_kwargs: object) -> object:
        return generated

    monkeypatch.setattr(
        handler_module.MessageHandler,
        "_generate_reply_core",
        _generate,
    )
    monkeypatch.setattr(handler_module, "get_config", _handler_config)
    monkeypatch.setattr(
        handler_module,
        "get_context_config",
        lambda: SimpleNamespace(bot_nickname="小鞠"),
    )
    return handler


async def test_attempt_reply_requires_wrapper_fingerprint_before_pending_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler_module = import_module(
        "komari_bot.plugins.komari_chat.handlers.message_handler"
    )
    handler = _attempt_handler(
        monkeypatch,
        handler_module,
        generated=import_module("komari_bot.plugins.komari_chat.services.chat_result").ReplyResult(
            content="回复正文",
            favorability_delta=1,
            favorability_reason="正常互动",
        ),
    )

    pending, _stored, failure = await handler._attempt_reply(
        bot_self_id="bot-1",
        adapter_name="onebot.v11",
        message=MessageSchema(
            user_id="user-1",
            user_nickname="测试",
            group_id="group-1",
            content="你好",
            timestamp=1.0,
            message_id="message-1",
        ),
        reply_to_message_id="message-1",
        image_urls=None,
        reply_context=None,
        reply_context_requested=False,
        reply_context_refetched=False,
        force_reply=True,
        reason="platform_summon",
        recall_session=fake_recall_session(handler.hindsight_memory),
        store_current=False,
    )

    assert pending is None
    assert failure is not None
    assert failure.error_type == "HindsightTargetFingerprintMissingError"


async def test_attempt_reply_copies_wrapper_fingerprint_into_pending_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler_module = import_module(
        "komari_bot.plugins.komari_chat.handlers.message_handler"
    )
    handler = _attempt_handler(
        monkeypatch,
        handler_module,
        generated=import_module("komari_bot.plugins.komari_chat.services.chat_result").ReplyResult(
            content="回复正文",
            favorability_delta=1,
            favorability_reason="正常互动",
            hindsight_target_fingerprint="d" * 64,
        ),
    )

    pending, _stored, failure = await handler._attempt_reply(
        bot_self_id="bot-1",
        adapter_name="onebot.v11",
        message=MessageSchema(
            user_id="user-1",
            user_nickname="测试",
            group_id="group-1",
            content="你好",
            timestamp=1.0,
            message_id="message-1",
        ),
        reply_to_message_id="message-1",
        image_urls=None,
        reply_context=None,
        reply_context_requested=False,
        reply_context_refetched=False,
        force_reply=True,
        reason="platform_summon",
        recall_session=fake_recall_session(handler.hindsight_memory),
        store_current=False,
    )

    assert failure is None
    assert pending is not None
    assert pending.hindsight_target_fingerprint == "d" * 64


class _ForbiddenFulfillment:
    async def has_fulfillment(self, _fulfillment_id: str) -> bool:
        msg = "debug 干跑不得触达回复履约持久化"
        raise AssertionError(msg)


async def test_debug_reply_runs_recall_only_and_never_retains_or_fulfills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler_module = import_module(
        "komari_bot.plugins.komari_chat.handlers.message_handler"
    )
    support = import_module("tests.komari_chat.hindsight_recall_support")
    memory = support.FakeHindsightMemory(response=support.recall_response())

    handler = handler_module.MessageHandler.__new__(handler_module.MessageHandler)
    handler.redis = _EmptyRedis()
    handler.hindsight_memory = memory
    handler.reply_fulfillment = _ForbiddenFulfillment()

    class _ChatService:
        async def generate(self, **_kwargs: object) -> object:
            return import_module(
                "komari_bot.plugins.komari_chat.services.chat_result"
            ).ReplyResult(
                content="调试回复",
                favorability_delta=0,
                favorability_reason="无变化",
            )

    handler.dsh_chat = _ChatService()
    monkeypatch.setattr(handler_module, "get_config", _handler_config)
    monkeypatch.setattr(
        handler_module,
        "get_context_config",
        lambda: SimpleNamespace(bot_nickname="小鞠"),
    )

    result = await handler.generate_debug_reply(
        group_id="group-1",
        user_id="user-1",
        user_nickname="测试",
        content="你好",
    )

    assert result.reply == "调试回复"
    assert all(not session.retain_calls for session in memory.sessions)
    assert not hasattr(result, "interaction_history")


async def _favorability(_user_id: str) -> object:
    return SimpleNamespace(favorability=0)
