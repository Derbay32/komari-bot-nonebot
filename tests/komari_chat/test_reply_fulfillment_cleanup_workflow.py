"""回复履约终态最小化与幂等证据清理的领域验收测试。"""

from __future__ import annotations

import asyncio
from importlib import import_module
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from nonebug import App


class _CleanupRepository:
    """只通过 workflow 驱动的终态保护期内存替身。"""

    def __init__(self, evidence: dict[str, dict[str, bool]]) -> None:
        self.evidence = evidence
        self.parents: dict[str, dict[str, Any]] = {}
        self.mark_error_for: set[str] = set()
        self.delete_error_for: set[str] = set()
        self.claimed_protection_days: list[int] = []

    def seed(
        self,
        fulfillment_id: str,
        *,
        resolved: bool,
        protection_elapsed: bool = True,
        evidence_cleared: bool = False,
    ) -> None:
        self.parents[fulfillment_id] = {
            "fulfillment_id": fulfillment_id,
            "resolved": resolved,
            "protection_elapsed": protection_elapsed,
            "idempotency_evidence_cleared_at": (
                "2026-01-01T00:00:00Z" if evidence_cleared else None
            ),
            "lease_owner": None,
        }
        self.evidence[fulfillment_id] = {
            "redis": evidence_cleared,
            "favorability": evidence_cleared,
        }

    def exists(self, fulfillment_id: str) -> bool:
        return fulfillment_id in self.parents

    def evidence_cleared(self, fulfillment_id: str) -> bool:
        parent = self.parents[fulfillment_id]
        return parent["idempotency_evidence_cleared_at"] is not None

    async def claim_terminal_cleanup_candidates(
        self,
        *,
        owner_token: str,
        limit: int,
        lease_seconds: int,
        protection_days: int,
    ) -> list[dict[str, Any]]:
        del lease_seconds
        self.claimed_protection_days.append(protection_days)
        claimed: list[dict[str, Any]] = []
        for parent in self.parents.values():
            if len(claimed) >= limit:
                break
            if (
                not parent["resolved"]
                or not parent["protection_elapsed"]
                or parent["lease_owner"] is not None
            ):
                continue
            parent["lease_owner"] = owner_token
            claimed.append(dict(parent))
        return claimed

    async def mark_idempotency_evidence_cleared(
        self,
        fulfillment_id: str,
        *,
        owner_token: str,
    ) -> bool:
        parent = self.parents[fulfillment_id]
        if parent["lease_owner"] != owner_token:
            return False
        evidence = self.evidence[fulfillment_id]
        assert evidence == {"redis": True, "favorability": True}
        if fulfillment_id in self.mark_error_for:
            self.mark_error_for.remove(fulfillment_id)
            raise ConnectionError("证据标记结果未知")
        parent["idempotency_evidence_cleared_at"] = "2026-01-01T00:00:00Z"
        return True

    async def delete_terminal_tombstone(
        self,
        fulfillment_id: str,
        *,
        owner_token: str,
    ) -> bool:
        parent = self.parents[fulfillment_id]
        if parent["lease_owner"] != owner_token:
            return False
        assert parent["resolved"] is True
        assert parent["protection_elapsed"] is True
        assert parent["idempotency_evidence_cleared_at"] is not None
        if fulfillment_id in self.delete_error_for:
            self.delete_error_for.remove(fulfillment_id)
            raise ConnectionError("删除结果未知")
        del self.parents[fulfillment_id]
        return True

    async def release_lease(
        self,
        fulfillment_id: str,
        *,
        owner_token: str,
    ) -> bool:
        parent = self.parents.get(fulfillment_id)
        if parent is None or parent["lease_owner"] != owner_token:
            return False
        parent["lease_owner"] = None
        return True


class _CleanupRedis:
    def __init__(self, evidence: dict[str, dict[str, bool]]) -> None:
        self.evidence = evidence
        self.error_for: set[str] = set()
        self.calls: list[str] = []

    async def delete_chat_commit_evidence(self, operation_id: str) -> int:
        self.calls.append(operation_id)
        if operation_id in self.error_for:
            self.error_for.remove(operation_id)
            raise ConnectionError("证据删除失败")
        state = self.evidence[operation_id]
        removed = int(state["redis"])
        state["redis"] = True
        return 0 if removed else 2


class _CleanupUserData:
    def __init__(self, evidence: dict[str, dict[str, bool]]) -> None:
        self.evidence = evidence
        self.error_for: set[str] = set()
        self.calls: list[str] = []

    async def delete_favorability_operation(self, operation_id: str) -> bool:
        fulfillment_id = operation_id.removesuffix(":favorability")
        self.calls.append(operation_id)
        if fulfillment_id in self.error_for:
            self.error_for.remove(fulfillment_id)
            raise ConnectionError("好感度证据删除失败")
        existed = not self.evidence[fulfillment_id]["favorability"]
        self.evidence[fulfillment_id]["favorability"] = True
        return existed


@pytest.fixture
def workflow_module(app: App) -> Any:
    del app
    return import_module(
        "komari_bot.plugins.komari_chat.services.reply_commitment_workflow"
    )


def _workflow(
    module: Any,
) -> tuple[Any, _CleanupRepository, _CleanupRedis, _CleanupUserData]:
    evidence: dict[str, dict[str, bool]] = {}
    repository = _CleanupRepository(evidence)
    redis = _CleanupRedis(evidence)
    user_data = _CleanupUserData(evidence)
    workflow = module.ReplyCommitmentWorkflow(
        repository=repository,
        redis=redis,
        proactive_reservation=SimpleNamespace(),
        user_data=user_data,
        config_getter=lambda: SimpleNamespace(
            reply_commit_batch_size=20,
            reply_commit_lease_seconds=30,
            reply_commit_max_attempts=3,
            reply_commit_retry_base_seconds=2,
            reply_commit_tombstone_retention_days=30,
        ),
    )
    return workflow, repository, redis, user_data


@pytest.mark.asyncio
async def test_cleanup_only_removes_resolved_terminal_after_protection(
    workflow_module: Any,
) -> None:
    workflow, repository, redis, user_data = _workflow(workflow_module)
    repository.seed("reply-completed", resolved=True)
    repository.seed("reply-not-delivered", resolved=True)
    repository.seed("reply-pending-confirmation", resolved=False)
    repository.seed("reply-needs-disposition", resolved=False)
    repository.seed(
        "reply-still-protected",
        resolved=True,
        protection_elapsed=False,
    )

    assert await workflow.cleanup_terminal_fulfillments() == 2

    assert repository.claimed_protection_days == [30]
    assert not repository.exists("reply-completed")
    assert not repository.exists("reply-not-delivered")
    assert repository.exists("reply-pending-confirmation")
    assert repository.exists("reply-needs-disposition")
    assert repository.exists("reply-still-protected")
    assert redis.calls == ["reply-completed", "reply-not-delivered"]
    assert user_data.calls == [
        "reply-completed:favorability",
        "reply-not-delivered:favorability",
    ]


@pytest.mark.asyncio
async def test_cleanup_retries_when_downstream_evidence_deletion_stops_midway(
    workflow_module: Any,
) -> None:
    workflow, repository, redis, user_data = _workflow(workflow_module)
    repository.seed("reply-mid-evidence", resolved=True)
    user_data.error_for.add("reply-mid-evidence")

    assert await workflow.cleanup_terminal_fulfillments() == 0

    assert repository.exists("reply-mid-evidence")
    assert repository.evidence_cleared("reply-mid-evidence") is False

    assert await workflow.cleanup_terminal_fulfillments() == 1

    assert not repository.exists("reply-mid-evidence")
    assert redis.calls == ["reply-mid-evidence", "reply-mid-evidence"]
    assert user_data.calls == [
        "reply-mid-evidence:favorability",
        "reply-mid-evidence:favorability",
    ]


@pytest.mark.asyncio
async def test_cleanup_retries_all_evidence_when_marker_result_is_unknown(
    workflow_module: Any,
) -> None:
    workflow, repository, redis, user_data = _workflow(workflow_module)
    repository.seed("reply-marker-unknown", resolved=True)
    repository.mark_error_for.add("reply-marker-unknown")

    assert await workflow.cleanup_terminal_fulfillments() == 0

    assert repository.exists("reply-marker-unknown")
    assert repository.evidence_cleared("reply-marker-unknown") is False

    assert await workflow.cleanup_terminal_fulfillments() == 1

    assert not repository.exists("reply-marker-unknown")
    assert redis.calls == ["reply-marker-unknown", "reply-marker-unknown"]
    assert user_data.calls == [
        "reply-marker-unknown:favorability",
        "reply-marker-unknown:favorability",
    ]


@pytest.mark.asyncio
async def test_cleanup_resumes_at_tombstone_after_persisted_evidence_marker(
    workflow_module: Any,
) -> None:
    workflow, repository, redis, user_data = _workflow(workflow_module)
    repository.seed("reply-delete-unknown", resolved=True)
    repository.delete_error_for.add("reply-delete-unknown")

    assert await workflow.cleanup_terminal_fulfillments() == 0

    assert repository.exists("reply-delete-unknown")
    assert repository.evidence_cleared("reply-delete-unknown") is True

    assert await workflow.cleanup_terminal_fulfillments() == 1

    assert not repository.exists("reply-delete-unknown")
    assert redis.calls == ["reply-delete-unknown"]
    assert user_data.calls == ["reply-delete-unknown:favorability"]


@pytest.mark.asyncio
async def test_cleanup_cancellation_propagates_without_deleting_tombstone(
    workflow_module: Any,
) -> None:
    workflow, repository, redis, _user_data = _workflow(workflow_module)
    repository.seed("reply-cleanup-cancelled", resolved=True)

    async def _cancel(_operation_id: str) -> int:
        raise asyncio.CancelledError

    redis.delete_chat_commit_evidence = _cancel

    with pytest.raises(asyncio.CancelledError):
        await workflow.cleanup_terminal_fulfillments()

    assert repository.exists("reply-cleanup-cancelled")
    assert repository.evidence_cleared("reply-cleanup-cancelled") is False
