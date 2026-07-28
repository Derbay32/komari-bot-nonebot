"""自定义提案发布状态机测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from komari_bot.plugins.komari_custom.models import Proposal
from komari_bot.plugins.komari_custom.publication_service import (
    ProposalPublicationDraft,
    ProposalPublicationError,
    ProposalPublicationInProgressError,
    ProposalPublicationReconciliationRequiredError,
    ProposalPublicationService,
    build_publication_key,
    extract_message_id,
)


class _MemoryPublicationRepository:
    def __init__(self) -> None:
        self.proposal: Proposal | None = None
        self.claimed_ids: list[int] = []
        self.fail_complete_once = False
        self._lock = asyncio.Lock()

    async def get_by_publication_key(self, publication_key: str) -> Proposal | None:
        if self.proposal is None or self.proposal.publication_key != publication_key:
            return None
        return self.proposal

    async def claim_publication(
        self,
        *,
        publication_key: str,
        publication_token: str,
        group_id: int,
        proposer_id: int,
        proposer_name: str | None,
        title: str,
        content: str,
        required_votes: int,
        expire_hours: int,
        lease_seconds: int,
    ) -> Proposal | None:
        del lease_seconds
        async with self._lock:
            now = datetime.now().astimezone()
            if self.proposal is None:
                self.proposal = Proposal(
                    id=1,
                    publication_key=publication_key,
                    publication_token=publication_token,
                    publication_started_at=now,
                    publication_attempts=1,
                    group_id=group_id,
                    proposer_id=proposer_id,
                    proposer_name=proposer_name,
                    title=title,
                    content=content,
                    status="publishing",
                    required_votes=required_votes,
                    created_at=now,
                    updated_at=now,
                    expired_at=now + timedelta(hours=expire_hours),
                )
            elif (
                self.proposal.status == "failed"
                and self.proposal.publication_error_code
                in {"send_rejected", "send_failed"}
            ):
                self.proposal = self.proposal.model_copy(
                    update={
                        "status": "publishing",
                        "publication_token": publication_token,
                        "publication_started_at": now,
                        "publication_attempts": self.proposal.publication_attempts + 1,
                        "publication_error_code": None,
                    }
                )
            else:
                return None
            self.claimed_ids.append(self.proposal.id)
            return self.proposal

    async def complete_publication(
        self,
        proposal_id: int,
        message_id: int,
        publication_token: str,
    ) -> Proposal | None:
        if self.fail_complete_once:
            self.fail_complete_once = False
            raise RuntimeError("模拟数据库回填失败")
        if (
            self.proposal is None
            or self.proposal.id != proposal_id
            or self.proposal.status != "publishing"
            or self.proposal.publication_token != publication_token
        ):
            return None
        self.proposal = self.proposal.model_copy(
            update={
                "status": "voting",
                "vote_message_id": message_id,
                "publication_token": None,
                "publication_error_code": None,
            }
        )
        return self.proposal

    async def recover_publication(
        self,
        publication_key: str,
        message_id: int,
    ) -> Proposal | None:
        if (
            self.proposal is None
            or self.proposal.publication_key != publication_key
            or self.proposal.status not in {"publishing", "failed"}
        ):
            return None
        self.proposal = self.proposal.model_copy(
            update={
                "status": "voting",
                "vote_message_id": message_id,
                "publication_token": None,
                "publication_error_code": None,
            }
        )
        return self.proposal

    async def mark_publication_failed(
        self,
        proposal_id: int,
        publication_token: str,
        error_code: str,
    ) -> Proposal | None:
        if (
            self.proposal is None
            or self.proposal.id != proposal_id
            or self.proposal.publication_token != publication_token
        ):
            return None
        self.proposal = self.proposal.model_copy(
            update={
                "status": "failed",
                "publication_token": None,
                "publication_error_code": error_code,
            }
        )
        return self.proposal


def _draft() -> ProposalPublicationDraft:
    return ProposalPublicationDraft(
        publication_key="stable-publication-key",
        group_id=100,
        proposer_id=200,
        proposer_name="测试用户",
        title="测试提案",
        content="正文",
        required_votes=3,
        expire_hours=2,
    )


async def _ignore_message_id(_message_id: int) -> None:
    return None


@pytest.mark.asyncio
async def test_definitive_send_rejection_can_retry_same_proposal() -> None:
    repository = _MemoryPublicationRepository()
    service = ProposalPublicationService(repository)
    send_calls = 0
    remembered_ids: list[int] = []

    async def _send(_proposal: Proposal) -> object:
        nonlocal send_calls
        send_calls += 1
        if send_calls == 1:
            msg = "模拟 QQ 发送失败"
            raise RuntimeError(msg)
        return {"message_id": 7788}

    async def _remember(message_id: int) -> None:
        remembered_ids.append(message_id)

    with pytest.raises(ProposalPublicationError) as exc_info:
        await service.publish(
            _draft(),
            remembered_message_id=None,
            send_message=_send,
            remember_message_id=_remember,
            is_definitive_send_failure=lambda _exc: True,
        )

    assert exc_info.value.error_code == "send_rejected"
    assert repository.proposal is not None
    assert repository.proposal.status == "failed"
    assert repository.proposal.publication_error_code == "send_rejected"
    first_proposal_id = repository.proposal.id

    published = await service.publish(
        _draft(),
        remembered_message_id=None,
        send_message=_send,
        remember_message_id=_remember,
        is_definitive_send_failure=lambda _exc: True,
    )

    assert published.id == first_proposal_id
    assert published.status == "voting"
    assert published.publication_attempts == 2
    assert repository.claimed_ids == [first_proposal_id, first_proposal_id]
    assert remembered_ids == [7788]


@pytest.mark.asyncio
async def test_missing_message_id_marks_publication_failed() -> None:
    repository = _MemoryPublicationRepository()
    service = ProposalPublicationService(repository)

    async def _send(_proposal: Proposal) -> object:
        return {"status": "ok"}

    with pytest.raises(
        ProposalPublicationReconciliationRequiredError
    ) as exc_info:
        await service.publish(
            _draft(),
            remembered_message_id=None,
            send_message=_send,
            remember_message_id=_ignore_message_id,
        )

    assert exc_info.value.error_code == "delivery_unknown"
    assert repository.proposal is not None
    assert repository.proposal.status == "failed"
    assert repository.proposal.publication_error_code == "delivery_unknown"


@pytest.mark.asyncio
async def test_ambiguous_send_failure_requires_reconciliation_without_resend() -> None:
    repository = _MemoryPublicationRepository()
    service = ProposalPublicationService(repository)
    send_calls = 0

    async def _send(_proposal: Proposal) -> object:
        nonlocal send_calls
        send_calls += 1
        msg = "模拟网络超时"
        raise TimeoutError(msg)

    with pytest.raises(ProposalPublicationReconciliationRequiredError):
        await service.publish(
            _draft(),
            remembered_message_id=None,
            send_message=_send,
            remember_message_id=_ignore_message_id,
        )
    with pytest.raises(ProposalPublicationReconciliationRequiredError):
        await service.publish(
            _draft(),
            remembered_message_id=None,
            send_message=_send,
            remember_message_id=_ignore_message_id,
        )

    assert send_calls == 1
    assert repository.proposal is not None
    assert repository.proposal.publication_error_code == "delivery_unknown"


@pytest.mark.asyncio
async def test_retry_recovers_remembered_message_without_sending_again() -> None:
    repository = _MemoryPublicationRepository()
    repository.fail_complete_once = True
    service = ProposalPublicationService(repository)
    remembered_ids: list[int] = []
    send_calls = 0

    async def _send(_proposal: Proposal) -> object:
        nonlocal send_calls
        send_calls += 1
        return {"message_id": 8899}

    async def _remember(message_id: int) -> None:
        remembered_ids.append(message_id)

    with pytest.raises(RuntimeError, match="数据库回填失败"):
        await service.publish(
            _draft(),
            remembered_message_id=None,
            send_message=_send,
            remember_message_id=_remember,
        )

    assert remembered_ids == [8899]
    assert repository.proposal is not None
    assert repository.proposal.status == "publishing"

    recovered = await service.publish(
        _draft(),
        remembered_message_id=8899,
        send_message=_send,
        remember_message_id=_remember,
    )

    assert recovered.status == "voting"
    assert recovered.vote_message_id == 8899
    assert send_calls == 1


@pytest.mark.asyncio
async def test_concurrent_confirm_only_allows_one_platform_send() -> None:
    repository = _MemoryPublicationRepository()
    service = ProposalPublicationService(repository)
    send_started = asyncio.Event()
    allow_send_to_finish = asyncio.Event()
    send_calls = 0

    async def _send(_proposal: Proposal) -> object:
        nonlocal send_calls
        send_calls += 1
        send_started.set()
        await allow_send_to_finish.wait()
        return {"message_id": 9900}

    first_task = asyncio.create_task(
        service.publish(
            _draft(),
            remembered_message_id=None,
            send_message=_send,
            remember_message_id=_ignore_message_id,
        )
    )
    await send_started.wait()

    with pytest.raises(ProposalPublicationInProgressError):
        await service.publish(
            _draft(),
            remembered_message_id=None,
            send_message=_send,
            remember_message_id=_ignore_message_id,
        )

    allow_send_to_finish.set()
    published = await first_task

    assert published.status == "voting"
    assert send_calls == 1


@pytest.mark.asyncio
async def test_already_published_retry_is_side_effect_free() -> None:
    repository = _MemoryPublicationRepository()
    service = ProposalPublicationService(repository)

    async def _send(_proposal: Proposal) -> object:
        return {"message_id": 1234}

    first = await service.publish(
        _draft(),
        remembered_message_id=None,
        send_message=_send,
        remember_message_id=_ignore_message_id,
    )

    async def _must_not_send(_proposal: Proposal) -> object:
        raise AssertionError("已发布提案不应再次发送")

    second = await service.publish(
        _draft(),
        remembered_message_id=None,
        send_message=_must_not_send,
        remember_message_id=_ignore_message_id,
    )

    assert second.id == first.id
    assert repository.claimed_ids == [first.id]


def test_publication_key_is_stable_and_hides_source_identifiers() -> None:
    first = build_publication_key(100, "200", "2026-07-16T12:00:00+08:00")
    second = build_publication_key(100, "200", "2026-07-16T12:00:00+08:00")

    assert first == second
    assert len(first) == 64
    assert "100" not in first
    assert "200" not in first


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"message_id": "123"}, 123),
        (SimpleNamespace(message_id=456), 456),
        ({"message_id": "invalid"}, None),
        ({}, None),
    ],
)
def test_extract_message_id(response: object, expected: int | None) -> None:
    assert extract_message_id(response) == expected
