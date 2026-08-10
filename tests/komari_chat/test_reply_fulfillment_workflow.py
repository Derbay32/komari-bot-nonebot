"""回复履约 workflow 的领域验收测试。"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from komari_bot.plugins.komari_memory.services.redis_manager import MessageSchema

if TYPE_CHECKING:
    from nonebug import App


class _FakeReplyFulfillmentRepository:
    """旧宽表 adapter 的内存替身，只向测试暴露领域投影。"""

    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}
        self.cleanup_count = 0

    @property
    def prepared_ids(self) -> set[str]:
        return {
            operation_id
            for operation_id, record in self._records.items()
            if record["status"] == "PREPARED"
        }

    @property
    def completed_ids(self) -> set[str]:
        return {
            operation_id
            for operation_id, record in self._records.items()
            if record["status"] == "COMPLETED"
        }

    @property
    def cancelled_ids(self) -> set[str]:
        return {
            operation_id
            for operation_id, record in self._records.items()
            if record["status"] == "CANCELLED"
        }

    def platform_message_id(self, operation_id: str) -> str | None:
        value = self._records[operation_id].get("platform_message_id")
        return str(value) if value is not None else None

    async def prepare(self, payload: Any) -> bool:
        if payload.operation_id in self._records:
            return False
        self._records[payload.operation_id] = {
            "operation_id": payload.operation_id,
            "request_trace_id": payload.request_trace_id,
            "source_message_id": payload.source_message_id,
            "group_id": payload.group_id,
            "user_id": payload.user_id,
            "user_nickname": payload.user_nickname,
            "bot_nickname": payload.bot_nickname,
            "reply_content": payload.reply_content,
            "reply_timestamp": payload.reply_timestamp,
            "favorability_delta": payload.favorability_delta,
            "favorability_reason": payload.favorability_reason,
            "interaction_history": payload.interaction_history,
            "proactive_reservation_id": payload.proactive_reservation_id,
            "proactive_cooldown_seconds": payload.proactive_cooldown_seconds,
            "global_interaction_enabled": payload.global_interaction_enabled,
            "global_interaction_trigger_size": payload.global_interaction_trigger_size,
            "status": "PREPARED",
            "proactive_confirmed_at": None,
            "favorability_applied_at": None,
            "ai_history_stored_at": None,
            "interaction_stored_at": None,
            "attempt_count": 0,
        }
        return True

    async def cancel_prepared(self, operation_id: str) -> bool:
        record = self._records[operation_id]
        if record["status"] != "PREPARED":
            return False
        record["status"] = "CANCELLED"
        return True

    async def mark_delivered(
        self,
        operation_id: str,
        *,
        platform_message_id: str | None = None,
    ) -> bool:
        record = self._records[operation_id]
        record["status"] = "DELIVERED"
        record["platform_message_id"] = platform_message_id
        return True

    async def claim_operation(
        self,
        operation_id: str,
        *,
        owner_token: str,
        lease_seconds: int,
    ) -> dict[str, Any] | None:
        del owner_token, lease_seconds
        record = self._records[operation_id]
        if record["status"] != "DELIVERED":
            return None
        record["status"] = "PROCESSING"
        record["attempt_count"] = int(record["attempt_count"]) + 1
        return record

    async def claim_pending(
        self,
        *,
        owner_token: str,
        limit: int,
        lease_seconds: int,
    ) -> list[dict[str, Any]]:
        del owner_token, lease_seconds
        claimed: list[dict[str, Any]] = []
        for record in self._records.values():
            if record["status"] == "DELIVERED" and len(claimed) < limit:
                record["status"] = "PROCESSING"
                record["attempt_count"] = int(record["attempt_count"]) + 1
                claimed.append(record)
        return claimed

    async def renew_lease(self, *_args: object, **_kwargs: object) -> bool:
        return True

    async def mark_step(
        self,
        operation_id: str,
        *,
        owner_token: str,
        step: str,
    ) -> bool:
        del owner_token
        columns = {
            "proactive_confirmed": "proactive_confirmed_at",
            "favorability_applied": "favorability_applied_at",
            "ai_history_stored": "ai_history_stored_at",
            "interaction_stored": "interaction_stored_at",
        }
        self._records[operation_id][columns[step]] = object()
        return True

    async def complete(self, operation_id: str, *, owner_token: str) -> bool:
        del owner_token
        record = self._records[operation_id]
        if not all(
            record[column] is not None
            for column in (
                "proactive_confirmed_at",
                "favorability_applied_at",
                "ai_history_stored_at",
                "interaction_stored_at",
            )
        ):
            return False
        record["status"] = "COMPLETED"
        return True

    async def mark_failure(
        self,
        operation_id: str,
        *,
        owner_token: str,
        error_code: str,
        max_attempts: int,
        retry_base_seconds: int,
    ) -> str:
        del owner_token, error_code, max_attempts, retry_base_seconds
        self._records[operation_id]["status"] = "DELIVERED"
        return "DELIVERED"

    async def cleanup_tombstones(self, *, retention_days: int) -> int:
        del retention_days
        self.cleanup_count += 1
        return 0


class _FakeRedis:
    def __init__(self) -> None:
        self.ai_operations: set[str] = set()
        self.interaction_operations: set[str] = set()
        self.fail_ai_once = False

    async def push_message_once(
        self,
        _group_id: str,
        _message: MessageSchema,
        *,
        operation_id: str,
        dedupe_ttl_seconds: int,
    ) -> bool:
        del dedupe_ttl_seconds
        if self.fail_ai_once:
            self.fail_ai_once = False
            message = "模拟 Redis 短暂故障"
            raise RuntimeError(message)
        inserted = operation_id not in self.ai_operations
        self.ai_operations.add(operation_id)
        return inserted

    async def push_global_interaction_once(
        self,
        *,
        user_id: str,
        record: dict[str, object],
        trigger_size: int,
        operation_id: str,
        dedupe_ttl_seconds: int,
    ) -> bool:
        del user_id, record, trigger_size, dedupe_ttl_seconds
        inserted = operation_id not in self.interaction_operations
        self.interaction_operations.add(operation_id)
        return inserted


class _FakeProactiveReservationService:
    def __init__(self) -> None:
        self.confirmed: set[str] = set()

    async def confirm(
        self,
        group_id: str,
        reservation_id: str,
        *,
        cooldown_seconds: int,
    ) -> None:
        del group_id, cooldown_seconds
        self.confirmed.add(reservation_id)


class _FakeReservation:
    def __init__(self) -> None:
        self.release_count = 0

    async def release(self) -> None:
        self.release_count += 1


class _FakeUserData:
    def __init__(self) -> None:
        self.operations: set[str] = set()
        self.application_count = 0
        self.cleanup_count = 0

    async def adjust_user_favorability(
        self,
        _user_id: str,
        _delta: int,
        *,
        operation_id: str,
    ) -> SimpleNamespace:
        if operation_id not in self.operations:
            self.operations.add(operation_id)
            self.application_count += 1
        return SimpleNamespace(before=0, delta=_delta, after=_delta)

    async def cleanup_favorability_operations(self, *, retention_days: int) -> int:
        del retention_days
        self.cleanup_count += 1
        return 0


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
    )


def _pending_reply(
    operation_id: str,
    *,
    reservation: _FakeReservation | None = None,
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
    _FakeReplyFulfillmentRepository,
    _FakeRedis,
    _FakeProactiveReservationService,
    _FakeUserData,
]:
    repository = _FakeReplyFulfillmentRepository()
    redis = _FakeRedis()
    proactive = _FakeProactiveReservationService()
    user_data = _FakeUserData()
    workflow = module.ReplyFulfillmentWorkflow(
        repository=repository,
        redis=redis,
        proactive_reservation=proactive,
        user_data=user_data,
        config_getter=_config,
    )
    return workflow, repository, redis, proactive, user_data


async def _send_success(_pending: object) -> dict[str, int]:
    return {"message_id": 7788}


def _definitive_failure(error: Exception) -> bool:
    return isinstance(error, PermissionError)


@pytest.mark.asyncio
async def test_fulfill_owns_delivery_and_all_post_delivery_commitments(
    workflow_module: Any,
) -> None:
    workflow, repository, redis, proactive, user_data = _workflow(workflow_module)
    pending = _pending_reply("reply-operation-1")

    await workflow.fulfill(
        pending,
        send_reply=_send_success,
        is_definitive_send_failure=_definitive_failure,
    )

    assert repository.completed_ids == {pending.operation_id}
    assert repository.platform_message_id(pending.operation_id) == "7788"
    assert proactive.confirmed == {"reservation-1"}
    assert user_data.application_count == 1
    assert redis.ai_operations == {pending.operation_id}
    assert redis.interaction_operations == {pending.operation_id}


@pytest.mark.asyncio
async def test_definitive_delivery_failure_terminates_without_commitments(
    workflow_module: Any,
) -> None:
    workflow, repository, redis, proactive, user_data = _workflow(workflow_module)
    reservation = _FakeReservation()
    pending = _pending_reply(
        "reply-operation-rejected",
        reservation=reservation,
    )

    async def _send_rejected(_pending: object) -> object:
        raise PermissionError("平台明确拒绝")

    with pytest.raises(Exception, match="平台明确拒绝"):
        await workflow.fulfill(
            pending,
            send_reply=_send_rejected,
            is_definitive_send_failure=_definitive_failure,
        )

    assert repository.cancelled_ids == {pending.operation_id}
    assert reservation.release_count == 1
    assert proactive.confirmed == set()
    assert user_data.application_count == 0
    assert redis.ai_operations == set()
    assert redis.interaction_operations == set()


@pytest.mark.asyncio
async def test_unknown_delivery_keeps_prepared_fulfillment_for_reconciliation(
    workflow_module: Any,
) -> None:
    workflow, repository, redis, proactive, user_data = _workflow(workflow_module)
    reservation = _FakeReservation()
    pending = _pending_reply(
        "reply-operation-unknown",
        reservation=reservation,
    )

    async def _send_unknown(_pending: object) -> object:
        raise TimeoutError("平台结果未知")

    with pytest.raises(Exception, match="平台结果未知"):
        await workflow.fulfill(
            pending,
            send_reply=_send_unknown,
            is_definitive_send_failure=_definitive_failure,
        )

    assert repository.prepared_ids == {pending.operation_id}
    assert reservation.release_count == 0
    assert proactive.confirmed == set()
    assert user_data.application_count == 0
    assert redis.ai_operations == set()
    assert redis.interaction_operations == set()


@pytest.mark.asyncio
async def test_recover_resumes_only_missing_commitments_without_resending(
    workflow_module: Any,
) -> None:
    workflow, repository, redis, proactive, user_data = _workflow(workflow_module)
    pending = _pending_reply("reply-operation-retry")
    send_count = 0

    async def _send(_pending: object) -> dict[str, int]:
        nonlocal send_count
        send_count += 1
        return {"message_id": 9001}

    redis.fail_ai_once = True
    await workflow.fulfill(
        pending,
        send_reply=_send,
        is_definitive_send_failure=_definitive_failure,
    )

    assert repository.completed_ids == set()
    assert user_data.application_count == 1
    assert await workflow.recover_pending() == 1
    assert repository.completed_ids == {pending.operation_id}
    assert send_count == 1
    assert user_data.application_count == 1
    assert proactive.confirmed == {"reservation-1"}
    assert redis.ai_operations == {pending.operation_id}
    assert redis.interaction_operations == {pending.operation_id}


@pytest.mark.asyncio
async def test_recover_preserves_lease_loss_and_failure_backoff_semantics(
    workflow_module: Any,
) -> None:
    workflow, repository, redis, _proactive, user_data = _workflow(workflow_module)
    pending = _pending_reply("reply-operation-lease")
    redis.fail_ai_once = True

    await workflow.fulfill(
        pending,
        send_reply=_send_success,
        is_definitive_send_failure=_definitive_failure,
    )
    first_application_count = user_data.application_count

    assert await workflow.recover_pending() == 1
    assert repository.completed_ids == {pending.operation_id}
    assert user_data.application_count == first_application_count


@pytest.mark.asyncio
async def test_recover_runs_terminal_identity_and_idempotency_cleanup(
    workflow_module: Any,
) -> None:
    workflow, repository, _redis, _proactive, user_data = _workflow(workflow_module)

    assert await workflow.recover_pending() == 0

    assert repository.cleanup_count == 1
    assert user_data.cleanup_count == 1


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
        assert "reply_commit_repository" not in source_file.read()
