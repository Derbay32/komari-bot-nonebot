"""回复履约独立承诺执行器的领域验收测试。"""

from __future__ import annotations

import asyncio
from importlib import import_module
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from redis.exceptions import RedisError

from komari_bot.plugins.komari_chat.reply_fulfillment_domain import (
    COMMITMENT_TYPES,
    ReplyFulfillmentConflictError,
)

if TYPE_CHECKING:
    from nonebug import App


class _CommitmentRepository:
    """只通过 workflow 驱动的父子履约内存替身。"""

    def __init__(self) -> None:
        self.parents: dict[str, dict[str, Any]] = {}
        self.children: dict[str, dict[str, dict[str, Any]]] = {}
        self.load_in_reverse = False
        self.completion_mark_lost_for: set[str] = set()
        self.completion_mark_unknown_for: set[str] = set()
        self.complete_parent_error_for: set[str] = set()
        self.claim_lease_seconds: list[int] = []
        self.failure_policies: list[tuple[str, int, int, int]] = []
        self.renew_count = 0
        self.renew_result: bool | None = None
        self.release_count = 0

    def seed(
        self,
        fulfillment_id: str,
        *,
        delivery_state: str = "DELIVERED",
        payloads: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        actual_payloads = payloads or _payloads()
        self.parents[fulfillment_id] = {
            "fulfillment_id": fulfillment_id,
            "delivery_state": delivery_state,
            "completed": False,
            "lease_owner": None,
        }
        self.children[fulfillment_id] = {
            commitment_type: {
                "commitment_type": commitment_type,
                "payload": payload,
                "state": "PENDING",
                "attempt_count": 0,
                "next_retry_due": True,
                "last_error_code": None,
                "completed": False,
            }
            for commitment_type, payload in actual_payloads.items()
        }

    def snapshot(self, fulfillment_id: str, commitment_type: str) -> SimpleNamespace:
        child = self.children[fulfillment_id][commitment_type]
        return SimpleNamespace(**child)

    def parent_completed(self, fulfillment_id: str) -> bool:
        return bool(self.parents[fulfillment_id]["completed"])

    def has_lease(self, fulfillment_id: str) -> bool:
        return self.parents[fulfillment_id]["lease_owner"] is not None

    def make_retries_due(self, fulfillment_id: str) -> None:
        for child in self.children[fulfillment_id].values():
            if child["state"] == "RETRY_WAIT":
                child["next_retry_due"] = True

    async def claim_pending(
        self,
        *,
        owner_token: str,
        limit: int,
        lease_seconds: int,
    ) -> list[dict[str, Any]]:
        self.claim_lease_seconds.append(lease_seconds)
        claimed: list[dict[str, Any]] = []
        for fulfillment_id, parent in self.parents.items():
            if len(claimed) >= limit:
                break
            children = self.children[fulfillment_id].values()
            has_due = any(
                child["state"] == "PENDING"
                or (child["state"] == "RETRY_WAIT" and child["next_retry_due"])
                for child in children
            )
            needs_parent_completion = all(
                child["state"] == "COMPLETED" for child in children
            )
            if (
                parent["delivery_state"] != "DELIVERED"
                or parent["completed"]
                or parent["lease_owner"] is not None
                or not (has_due or needs_parent_completion)
            ):
                continue
            parent["lease_owner"] = owner_token
            claimed.append({"fulfillment_id": fulfillment_id})
        return claimed

    async def load_claimed_commitments(
        self,
        fulfillment_id: str,
        *,
        owner_token: str,
    ) -> list[dict[str, Any]] | None:
        parent = self.parents[fulfillment_id]
        if parent["lease_owner"] != owner_token:
            return None
        rows = [
            dict(child)
            for child in self.children[fulfillment_id].values()
            if child["state"] == "PENDING"
            or (child["state"] == "RETRY_WAIT" and child["next_retry_due"])
        ]
        if self.load_in_reverse:
            rows.reverse()
        return rows

    async def renew_lease(
        self,
        fulfillment_id: str,
        *,
        owner_token: str,
        lease_seconds: int,
    ) -> bool:
        del lease_seconds
        self.renew_count += 1
        if self.renew_result is not None:
            return self.renew_result
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
        self.release_count += 1
        parent["lease_owner"] = None
        return True

    async def mark_commitment_completed(
        self,
        fulfillment_id: str,
        *,
        commitment_type: str,
        owner_token: str,
    ) -> bool:
        parent = self.parents[fulfillment_id]
        if commitment_type in self.completion_mark_unknown_for:
            self.completion_mark_unknown_for.remove(commitment_type)
            raise ConnectionError("完成标记结果未知")
        if commitment_type in self.completion_mark_lost_for:
            self.completion_mark_lost_for.remove(commitment_type)
            parent["lease_owner"] = None
            return False
        if parent["lease_owner"] != owner_token:
            return False
        child = self.children[fulfillment_id][commitment_type]
        child.update(
            state="COMPLETED",
            attempt_count=int(child["attempt_count"]) + 1,
            next_retry_due=False,
            last_error_code=None,
            payload=None,
            completed=True,
        )
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
        parent = self.parents[fulfillment_id]
        if parent["lease_owner"] != owner_token:
            return None
        child = self.children[fulfillment_id][commitment_type]
        attempts = int(child["attempt_count"]) + 1
        state = "FAILED" if attempts >= max_attempts else "RETRY_WAIT"
        child.update(
            state=state,
            attempt_count=attempts,
            next_retry_due=False,
            last_error_code=error_code,
        )
        self.failure_policies.append(
            (
                commitment_type,
                max_attempts,
                retry_base_seconds,
                retry_max_seconds,
            )
        )
        return state

    async def complete_fulfillment(
        self,
        fulfillment_id: str,
        *,
        owner_token: str,
    ) -> bool:
        parent = self.parents[fulfillment_id]
        if fulfillment_id in self.complete_parent_error_for:
            self.complete_parent_error_for.remove(fulfillment_id)
            raise ConnectionError("父完成标记结果未知")
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


class _ProactiveReservation:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.error: BaseException | None = None
        self.calls = 0

    async def confirm(
        self,
        _group_id: str,
        _reservation_id: str,
        *,
        cooldown_seconds: int,
    ) -> None:
        del cooldown_seconds
        self.calls += 1
        self.events.append("proactive_reply_confirmation")
        if self.error is not None:
            raise self.error


class _UserData:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.error: BaseException | None = None
        self.calls = 0
        self.application_count = 0
        self.started = asyncio.Event()
        self.resume = asyncio.Event()
        self.block = False
        self._operations: set[str] = set()

    async def adjust_user_favorability(
        self,
        _user_id: str,
        _delta: int,
        *,
        operation_id: str,
    ) -> SimpleNamespace:
        self.calls += 1
        self.events.append("favorability_adjustment")
        self.started.set()
        if self.block:
            await self.resume.wait()
        if self.error is not None:
            raise self.error
        if operation_id not in self._operations:
            self._operations.add(operation_id)
            self.application_count += 1
        return SimpleNamespace(before=0, delta=1, after=1)


class _Redis:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.assistant_error: BaseException | None = None
        self.interaction_error: BaseException | None = None
        self.assistant_calls = 0
        self.interaction_calls = 0
        self.assistant_ttls: list[int | None] = []
        self.interaction_ttls: list[int | None] = []
        self._assistant_operations: set[str] = set()
        self._interaction_operations: set[str] = set()

    async def push_message_once(
        self,
        _group_id: str,
        _message: object,
        *,
        operation_id: str,
        dedupe_ttl_seconds: int | None = None,
    ) -> bool:
        self.assistant_ttls.append(dedupe_ttl_seconds)
        self.assistant_calls += 1
        self.events.append("assistant_reply_history")
        if self.assistant_error is not None:
            raise self.assistant_error
        inserted = operation_id not in self._assistant_operations
        self._assistant_operations.add(operation_id)
        return inserted

    async def push_global_interaction_once(
        self,
        *,
        user_id: str,
        record: dict[str, object],
        trigger_size: int,
        operation_id: str,
        dedupe_ttl_seconds: int | None = None,
    ) -> bool:
        del user_id, record, trigger_size
        self.interaction_ttls.append(dedupe_ttl_seconds)
        self.interaction_calls += 1
        self.events.append("interaction_history")
        if self.interaction_error is not None:
            raise self.interaction_error
        inserted = operation_id not in self._interaction_operations
        self._interaction_operations.add(operation_id)
        return inserted


@pytest.fixture
def workflow_module(app: App) -> Any:
    del app
    return import_module(
        "komari_bot.plugins.komari_chat.services.reply_commitment_workflow"
    )


def _payloads() -> dict[str, dict[str, Any]]:
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
            "record": {
                "event": "用户发言",
                "result": "机器人回复",
                "emotion": "平静",
            },
        },
    }


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        reply_fulfillment_batch_size=20,
        reply_fulfillment_lease_seconds=30,
        reply_fulfillment_max_attempts=3,
        reply_fulfillment_retry_base_seconds=2,
        reply_fulfillment_retry_max_seconds=7,
        reply_fulfillment_tombstone_retention_days=30,
    )


def _workflow(
    module: Any,
    repository: _CommitmentRepository,
    config: SimpleNamespace,
    *,
    config_getter: Any = None,
) -> tuple[Any, list[str], _Redis, _ProactiveReservation, _UserData]:
    events: list[str] = []
    redis = _Redis(events)
    proactive = _ProactiveReservation(events)
    user_data = _UserData(events)
    workflow = module.ReplyCommitmentWorkflow(
        repository=repository,
        redis=redis,
        proactive_reservation=proactive,
        user_data=user_data,
        config_getter=config_getter or (lambda: config),
    )
    return workflow, events, redis, proactive, user_data


@pytest.mark.asyncio
async def test_all_commitments_complete_in_domain_order_and_minimize_payloads(
    workflow_module: Any,
) -> None:
    repository = _CommitmentRepository()
    repository.seed("reply-success")
    repository.load_in_reverse = True
    config = _config()
    workflow, events, _redis, _proactive, _user_data = _workflow(
        workflow_module,
        repository,
        config,
    )

    assert await workflow.recover_pending() == 1

    assert events == list(COMMITMENT_TYPES)
    assert repository.parent_completed("reply-success") is True
    assert repository.has_lease("reply-success") is False
    for commitment_type in COMMITMENT_TYPES:
        child = repository.snapshot("reply-success", commitment_type)
        assert child.completed is True
        assert child.attempt_count == 1
        assert child.payload is None


@pytest.mark.asyncio
async def test_transient_failure_does_not_block_siblings_and_retries_only_missing_item(
    workflow_module: Any,
) -> None:
    repository = _CommitmentRepository()
    repository.seed("reply-retry")
    config = _config()
    workflow, events, redis, _proactive, user_data = _workflow(
        workflow_module,
        repository,
        config,
    )
    user_data.error = TimeoutError("不得进入稳定错误码的内部细节")

    assert await workflow.recover_pending() == 0

    assert events == list(COMMITMENT_TYPES)
    failed = repository.snapshot("reply-retry", "favorability_adjustment")
    assert failed.state == "RETRY_WAIT"
    assert failed.attempt_count == 1
    assert failed.last_error_code == "transient_timeout"
    assert "内部细节" not in failed.last_error_code
    assert repository.has_lease("reply-retry") is False
    assert redis.assistant_calls == 1
    assert redis.interaction_calls == 1

    user_data.error = None
    repository.make_retries_due("reply-retry")
    events.clear()

    assert await workflow.recover_pending() == 1

    assert events == ["favorability_adjustment"]
    assert user_data.application_count == 1
    assert repository.parent_completed("reply-retry") is True
    for commitment_type in COMMITMENT_TYPES:
        assert repository.snapshot("reply-retry", commitment_type).completed is True


@pytest.mark.asyncio
async def test_multiple_failures_keep_independent_budgets_and_use_latest_policy(
    workflow_module: Any,
) -> None:
    repository = _CommitmentRepository()
    repository.seed("reply-multi-retry")
    config = _config()
    workflow, events, redis, proactive, _user_data = _workflow(
        workflow_module,
        repository,
        config,
    )
    proactive.error = ConnectionError("主动回复服务暂不可用")
    redis.assistant_error = RedisError("Redis 暂不可用")

    assert await workflow.recover_pending() == 0

    proactive_child = repository.snapshot(
        "reply-multi-retry", "proactive_reply_confirmation"
    )
    assistant_child = repository.snapshot(
        "reply-multi-retry", "assistant_reply_history"
    )
    assert proactive_child.state == "RETRY_WAIT"
    assert assistant_child.state == "RETRY_WAIT"
    assert proactive_child.attempt_count == 1
    assert assistant_child.attempt_count == 1
    assert repository.snapshot("reply-multi-retry", "favorability_adjustment").completed
    assert repository.snapshot("reply-multi-retry", "interaction_history").completed

    config.reply_fulfillment_max_attempts = 1
    config.reply_fulfillment_retry_base_seconds = 10
    config.reply_fulfillment_retry_max_seconds = 11
    config.reply_fulfillment_lease_seconds = 60
    repository.make_retries_due("reply-multi-retry")
    events.clear()

    assert await workflow.recover_pending() == 0

    assert events == [
        "proactive_reply_confirmation",
        "assistant_reply_history",
    ]
    assert (
        repository.snapshot("reply-multi-retry", "proactive_reply_confirmation").state
        == "FAILED"
    )
    assert (
        repository.snapshot("reply-multi-retry", "assistant_reply_history").state
        == "FAILED"
    )
    assert repository.parent_completed("reply-multi-retry") is False
    assert repository.claim_lease_seconds[-1] == 60
    assert repository.failure_policies[-2:] == [
        ("proactive_reply_confirmation", 1, 10, 11),
        ("assistant_reply_history", 1, 10, 11),
    ]
    assert await workflow.recover_pending() == 0
    assert proactive.calls == 2
    assert redis.assistant_calls == 2


@pytest.mark.asyncio
async def test_retry_policy_is_read_when_each_failure_is_recorded(
    workflow_module: Any,
) -> None:
    repository = _CommitmentRepository()
    repository.seed("reply-live-policy")
    config = _config()
    updated_config = _config()
    updated_config.reply_fulfillment_max_attempts = 9
    updated_config.reply_fulfillment_retry_base_seconds = 10
    updated_config.reply_fulfillment_retry_max_seconds = 11
    reads = 0

    def _get_config() -> SimpleNamespace:
        nonlocal reads
        reads += 1
        return config if reads <= 2 else updated_config

    workflow, _events, _redis, proactive, user_data = _workflow(
        workflow_module,
        repository,
        config,
        config_getter=_get_config,
    )
    proactive.error = TimeoutError("主动回复超时")
    user_data.error = TimeoutError("好感度超时")

    assert await workflow.recover_pending() == 0

    assert repository.failure_policies[:2] == [
        ("proactive_reply_confirmation", 3, 2, 7),
        ("favorability_adjustment", 9, 10, 11),
    ]


@pytest.mark.asyncio
async def test_invalid_payload_enters_disposition_without_blocking_later_commitment(
    workflow_module: Any,
) -> None:
    repository = _CommitmentRepository()
    payloads = _payloads()
    payloads["assistant_reply_history"] = {
        "group_id": "group-1",
        "bot_nickname": "小鞠",
    }
    repository.seed("reply-invalid", payloads=payloads)
    config = _config()
    workflow, events, redis, _proactive, _user_data = _workflow(
        workflow_module,
        repository,
        config,
    )

    assert await workflow.recover_pending() == 0

    child = repository.snapshot("reply-invalid", "assistant_reply_history")
    assert child.state == "FAILED"
    assert child.attempt_count == 1
    assert child.last_error_code == "invalid_payload"
    assert child.payload is not None
    assert redis.assistant_calls == 0
    assert redis.interaction_calls == 1
    assert events == [
        "proactive_reply_confirmation",
        "favorability_adjustment",
        "interaction_history",
    ]
    assert repository.parent_completed("reply-invalid") is False


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (ReplyFulfillmentConflictError("冲突正文不可泄漏"), "idempotency_conflict"),
        (TypeError("下游协议正文不可泄漏"), "protocol_violation"),
    ],
)
@pytest.mark.asyncio
async def test_known_permanent_errors_enter_disposition_immediately(
    workflow_module: Any,
    error: Exception,
    expected_code: str,
) -> None:
    repository = _CommitmentRepository()
    repository.seed(f"reply-permanent-{expected_code}")
    config = _config()
    workflow, events, _redis, _proactive, user_data = _workflow(
        workflow_module,
        repository,
        config,
    )
    user_data.error = error

    assert await workflow.recover_pending() == 0

    child = repository.snapshot(
        f"reply-permanent-{expected_code}", "favorability_adjustment"
    )
    assert child.state == "FAILED"
    assert child.attempt_count == 1
    assert child.last_error_code == expected_code
    assert "正文" not in child.last_error_code
    assert events == list(COMMITMENT_TYPES)


@pytest.mark.asyncio
async def test_value_error_idempotency_conflict_enters_disposition_immediately(
    workflow_module: Any,
) -> None:
    repository = _CommitmentRepository()
    repository.seed("reply-ledger-conflict")
    config = _config()
    workflow, _events, _redis, _proactive, user_data = _workflow(
        workflow_module,
        repository,
        config,
    )
    error: Any = ValueError("好感度 operation_id 与既有请求载荷冲突")
    error.error_code = "idempotency_conflict"
    user_data.error = error

    assert await workflow.recover_pending() == 0

    child = repository.snapshot("reply-ledger-conflict", "favorability_adjustment")
    assert child.state == "FAILED"
    assert child.attempt_count == 1
    assert child.last_error_code == "idempotency_conflict"


@pytest.mark.asyncio
async def test_dynamic_service_shutdown_retries_without_revoking_commitment(
    workflow_module: Any,
) -> None:
    repository = _CommitmentRepository()
    repository.seed("reply-service-disabled")
    config = _config()
    workflow, _events, _redis, _proactive, user_data = _workflow(
        workflow_module,
        repository,
        config,
    )
    error: Any = RuntimeError("UserDataDB 连接池未初始化")
    error.error_code = "service_unavailable"
    user_data.error = error

    assert await workflow.recover_pending() == 0

    child = repository.snapshot("reply-service-disabled", "favorability_adjustment")
    assert child.state == "RETRY_WAIT"
    assert child.completed is False
    assert child.last_error_code == "service_unavailable"
    assert repository.parents["reply-service-disabled"]["delivery_state"] == "DELIVERED"
    assert repository.parent_completed("reply-service-disabled") is False


@pytest.mark.asyncio
async def test_lease_loss_stops_the_executor_and_idempotent_recovery_finishes(
    workflow_module: Any,
) -> None:
    repository = _CommitmentRepository()
    repository.seed("reply-lease-lost")
    repository.completion_mark_lost_for.add("favorability_adjustment")
    config = _config()
    workflow, events, redis, proactive, user_data = _workflow(
        workflow_module,
        repository,
        config,
    )

    assert await workflow.recover_pending() == 0

    assert events == [
        "proactive_reply_confirmation",
        "favorability_adjustment",
    ]
    assert (
        repository.snapshot("reply-lease-lost", "favorability_adjustment").attempt_count
        == 0
    )
    assert repository.failure_policies == []
    assert redis.assistant_calls == 0
    assert redis.interaction_calls == 0

    events.clear()
    assert await workflow.recover_pending() == 1

    assert events == [
        "favorability_adjustment",
        "assistant_reply_history",
        "interaction_history",
    ]
    assert proactive.calls == 1
    assert user_data.calls == 2
    assert user_data.application_count == 1
    assert repository.parent_completed("reply-lease-lost") is True


@pytest.mark.asyncio
async def test_completion_mark_unknown_backs_off_item_and_continues_batch(
    workflow_module: Any,
) -> None:
    repository = _CommitmentRepository()
    repository.seed("reply-mark-unknown")
    repository.seed("reply-after-unknown")
    repository.completion_mark_unknown_for.add("favorability_adjustment")
    config = _config()
    workflow, _events, redis, _proactive, _user_data = _workflow(
        workflow_module,
        repository,
        config,
    )

    assert await workflow.recover_pending() == 1

    uncertain = repository.snapshot("reply-mark-unknown", "favorability_adjustment")
    assert uncertain.state == "RETRY_WAIT"
    assert uncertain.attempt_count == 1
    assert uncertain.last_error_code == "connection_error"
    assert repository.snapshot(
        "reply-mark-unknown", "assistant_reply_history"
    ).completed
    assert repository.snapshot("reply-mark-unknown", "interaction_history").completed
    assert redis.assistant_calls == 2
    assert redis.interaction_calls == 2
    assert repository.parent_completed("reply-mark-unknown") is False
    assert repository.has_lease("reply-mark-unknown") is False
    assert repository.parent_completed("reply-after-unknown") is True


@pytest.mark.asyncio
async def test_parent_completion_unknown_is_reclaimed_without_repeating_children(
    workflow_module: Any,
) -> None:
    repository = _CommitmentRepository()
    repository.seed("reply-parent-unknown")
    repository.complete_parent_error_for.add("reply-parent-unknown")
    config = _config()
    workflow, events, _redis, _proactive, _user_data = _workflow(
        workflow_module,
        repository,
        config,
    )

    assert await workflow.recover_pending() == 0
    assert events == list(COMMITMENT_TYPES)
    assert repository.parent_completed("reply-parent-unknown") is False
    assert repository.has_lease("reply-parent-unknown") is False

    events.clear()
    assert await workflow.recover_pending() == 1

    assert events == []
    assert repository.parent_completed("reply-parent-unknown") is True


@pytest.mark.asyncio
async def test_long_running_commitment_renews_the_single_parent_lease(
    workflow_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _CommitmentRepository()
    repository.seed("reply-heartbeat")
    config = _config()
    config.reply_fulfillment_lease_seconds = 1
    workflow, _events, _redis, _proactive, user_data = _workflow(
        workflow_module,
        repository,
        config,
    )
    user_data.block = True

    original_sleep = asyncio.sleep

    async def _fast_sleep(_delay: float) -> None:
        await original_sleep(0)

    monkeypatch.setattr(workflow_module.asyncio, "sleep", _fast_sleep)
    task = asyncio.create_task(workflow.recover_pending())
    # 超时护栏：workflow 若在执行承诺前失败，测试必须快速红灯而非永久挂起
    await asyncio.wait_for(user_data.started.wait(), timeout=5)
    for _ in range(20):
        if repository.renew_count:
            break
        await original_sleep(0)
    user_data.resume.set()

    assert await asyncio.wait_for(task, timeout=5) == 1
    assert repository.renew_count >= 1
    assert repository.has_lease("reply-heartbeat") is False

    renew_count = repository.renew_count
    await original_sleep(0)
    assert repository.renew_count == renew_count


@pytest.mark.asyncio
async def test_heartbeat_loss_stops_before_marking_and_releases_owned_lease(
    workflow_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _CommitmentRepository()
    repository.seed("reply-heartbeat-lost")
    repository.renew_result = False
    config = _config()
    config.reply_fulfillment_lease_seconds = 1
    workflow, _events, _redis, _proactive, user_data = _workflow(
        workflow_module,
        repository,
        config,
    )
    user_data.block = True

    original_sleep = asyncio.sleep

    async def _fast_sleep(_delay: float) -> None:
        await original_sleep(0)

    monkeypatch.setattr(workflow_module.asyncio, "sleep", _fast_sleep)
    task = asyncio.create_task(workflow.recover_pending())
    # 超时护栏：workflow 若在执行承诺前失败，测试必须快速红灯而非永久挂起
    await asyncio.wait_for(user_data.started.wait(), timeout=5)
    for _ in range(20):
        if repository.renew_count:
            break
        await original_sleep(0)
    user_data.resume.set()

    assert await asyncio.wait_for(task, timeout=5) == 0
    assert repository.renew_count >= 1
    child = repository.snapshot("reply-heartbeat-lost", "favorability_adjustment")
    assert child.state == "RETRY_WAIT"
    assert child.attempt_count == 1
    assert child.last_error_code == "lease_lost"
    assert child.completed is False
    assert repository.release_count == 1
    assert repository.has_lease("reply-heartbeat-lost") is False


@pytest.mark.asyncio
async def test_cancellation_propagates_without_failure_budget_and_can_recover(
    workflow_module: Any,
) -> None:
    repository = _CommitmentRepository()
    repository.seed("reply-cancelled")
    config = _config()
    workflow, events, _redis, proactive, user_data = _workflow(
        workflow_module,
        repository,
        config,
    )
    user_data.error = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await workflow.recover_pending()

    child = repository.snapshot("reply-cancelled", "favorability_adjustment")
    assert child.attempt_count == 0
    assert child.last_error_code is None
    assert repository.failure_policies == []
    assert repository.has_lease("reply-cancelled") is False

    user_data.error = None
    events.clear()
    assert await workflow.recover_pending() == 1

    assert proactive.calls == 1
    assert events == [
        "favorability_adjustment",
        "assistant_reply_history",
        "interaction_history",
    ]


@pytest.mark.asyncio
async def test_only_delivered_fulfillments_run_their_frozen_subset(
    workflow_module: Any,
) -> None:
    repository = _CommitmentRepository()
    core_payloads = _payloads()
    core_payloads.pop("proactive_reply_confirmation")
    core_payloads.pop("interaction_history")
    repository.seed(
        "reply-pending-confirmation",
        delivery_state="PENDING_CONFIRMATION",
        payloads=core_payloads,
    )
    repository.seed("reply-core-only", payloads=core_payloads)
    config = _config()
    workflow, events, redis, proactive, _user_data = _workflow(
        workflow_module,
        repository,
        config,
    )

    assert await workflow.recover_pending() == 1

    assert events == [
        "favorability_adjustment",
        "assistant_reply_history",
    ]
    assert proactive.calls == 0
    assert redis.interaction_calls == 0
    assert repository.parent_completed("reply-core-only") is True
    assert repository.parent_completed("reply-pending-confirmation") is False


@pytest.mark.asyncio
async def test_unresolved_fulfillment_uses_persistent_redis_idempotency_evidence(
    workflow_module: Any,
) -> None:
    repository = _CommitmentRepository()
    repository.seed("reply-persistent-evidence")
    config = _config()
    workflow, _events, redis, _proactive, user_data = _workflow(
        workflow_module,
        repository,
        config,
    )
    user_data.error = TimeoutError("保持履约未解决")

    assert await workflow.recover_pending() == 0

    assert redis.assistant_ttls == [None]
    assert redis.interaction_ttls == [None]
    assert repository.parent_completed("reply-persistent-evidence") is False
