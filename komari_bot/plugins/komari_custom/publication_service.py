"""知识提案发布状态机与故障恢复。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from nonebot import logger

from komari_bot.llm.content_budget import (
    PROPOSAL_CONTENT_TEXT_BUDGET,
    TITLE_TEXT_BUDGET,
    normalize_required_text,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .models import Proposal

PUBLICATION_LEASE_SECONDS = 300
_PUBLISHED_STATUSES = frozenset({"voting", "approving", "approved"})


@dataclass(frozen=True)
class ProposalPublicationDraft:
    """创建发布记录所需的稳定输入。"""

    publication_key: str
    group_id: int
    proposer_id: int
    proposer_name: str | None
    title: str
    content: str
    required_votes: int
    expire_hours: int

    def __post_init__(self) -> None:
        """防止绕过命令编辑路径写入超预算提案。"""
        object.__setattr__(
            self,
            "title",
            normalize_required_text(
                self.title,
                label="提案标题",
                budget=TITLE_TEXT_BUDGET,
            ),
        )
        object.__setattr__(
            self,
            "content",
            normalize_required_text(
                self.content,
                label="提案正文",
                budget=PROPOSAL_CONTENT_TEXT_BUDGET,
            ),
        )


class PublicationRepository(Protocol):
    """发布状态机所需的最小仓库协议。"""

    async def get_by_publication_key(self, publication_key: str) -> Proposal | None: ...

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
    ) -> Proposal | None: ...

    async def complete_publication(
        self,
        proposal_id: int,
        message_id: int,
        publication_token: str,
    ) -> Proposal | None: ...

    async def recover_publication(
        self,
        publication_key: str,
        message_id: int,
    ) -> Proposal | None: ...

    async def mark_publication_failed(
        self,
        proposal_id: int,
        publication_token: str,
        error_code: str,
    ) -> Proposal | None: ...


class ProposalPublicationError(RuntimeError):
    """提案发布失败，并携带不含底层异常详情的稳定错误码。"""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


class ProposalPublicationInProgressError(ProposalPublicationError):
    """同一编辑会话已有未过租约的发布者。"""


class ProposalPublicationReconciliationRequiredError(ProposalPublicationError):
    """平台投递结果不确定，禁止自动重发并等待人工对账。"""


def build_publication_key(group_id: int, user_id: str, created_at: str) -> str:
    """由稳定编辑会话属性生成不暴露用户信息的幂等键。"""
    raw_key = f"{group_id}:{user_id}:{created_at}".encode()
    return hashlib.sha256(raw_key).hexdigest()


def extract_message_id(sent: object) -> int | None:
    """兼容 OneBot 字典或对象响应并提取消息 ID。"""
    value = sent.get("message_id") if isinstance(sent, dict) else getattr(
        sent, "message_id", None
    )
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class ProposalPublicationService:
    """以数据库认领和编辑会话恢复点编排一次提案发布。"""

    def __init__(self, repository: PublicationRepository) -> None:
        self._repository = repository

    async def publish(
        self,
        draft: ProposalPublicationDraft,
        *,
        remembered_message_id: int | None,
        send_message: Callable[[Proposal], Awaitable[object]],
        remember_message_id: Callable[[int], Awaitable[None]],
        is_definitive_send_failure: Callable[[Exception], bool] | None = None,
    ) -> Proposal:
        """发布或恢复同一提案，成功时保证记录已进入投票及以后状态。"""
        current = await self._repository.get_by_publication_key(
            draft.publication_key
        )
        published = self._published_proposal(current)
        if published is not None:
            return published

        if current is not None and remembered_message_id is not None:
            recovered = await self._repository.recover_publication(
                draft.publication_key,
                remembered_message_id,
            )
            if recovered is not None:
                return recovered

        if self._requires_reconciliation(current):
            raise ProposalPublicationReconciliationRequiredError(
                "publication_reconciliation_required"
            )

        publication_token = uuid4().hex
        claimed = await self._repository.claim_publication(
            publication_key=draft.publication_key,
            publication_token=publication_token,
            group_id=draft.group_id,
            proposer_id=draft.proposer_id,
            proposer_name=draft.proposer_name,
            title=draft.title,
            content=draft.content,
            required_votes=draft.required_votes,
            expire_hours=draft.expire_hours,
            lease_seconds=PUBLICATION_LEASE_SECONDS,
        )
        if claimed is None:
            current = await self._repository.get_by_publication_key(
                draft.publication_key
            )
            published = self._published_proposal(current)
            if published is not None:
                return published
            if self._requires_reconciliation(current):
                raise ProposalPublicationReconciliationRequiredError(
                    "publication_reconciliation_required"
                )
            raise ProposalPublicationInProgressError("publication_in_progress")

        try:
            sent = await send_message(claimed)
        except Exception as exc:
            definitive_failure = False
            if is_definitive_send_failure is not None:
                try:
                    definitive_failure = is_definitive_send_failure(exc)
                except Exception:
                    logger.exception(
                        "[KomariCustom] 平台发送失败分类器异常，按投递结果未知处理"
                    )
            error_code = "send_rejected" if definitive_failure else "delivery_unknown"
            await self._repository.mark_publication_failed(
                claimed.id,
                publication_token,
                error_code,
            )
            if definitive_failure:
                raise ProposalPublicationError(error_code) from exc
            raise ProposalPublicationReconciliationRequiredError(error_code) from exc

        message_id = extract_message_id(sent)
        if message_id is None:
            await self._repository.mark_publication_failed(
                claimed.id,
                publication_token,
                "delivery_unknown",
            )
            raise ProposalPublicationReconciliationRequiredError("delivery_unknown")

        try:
            await remember_message_id(message_id)
        except Exception as exc:
            logger.warning(
                "[KomariCustom] 暂存发布消息 ID 失败，继续尝试数据库提交: error_type={}",
                type(exc).__name__,
            )

        completed = await self._repository.complete_publication(
            claimed.id,
            message_id,
            publication_token,
        )
        if completed is not None:
            return completed

        current = await self._repository.get_by_publication_key(draft.publication_key)
        published = self._published_proposal(current, expected_message_id=message_id)
        if published is not None:
            return published
        raise ProposalPublicationError("publication_state_conflict")

    @staticmethod
    def _published_proposal(
        proposal: Proposal | None,
        *,
        expected_message_id: int | None = None,
    ) -> Proposal | None:
        if proposal is None or proposal.status not in _PUBLISHED_STATUSES:
            return None
        if proposal.vote_message_id is None:
            return None
        if (
            expected_message_id is not None
            and proposal.vote_message_id != expected_message_id
        ):
            return None
        return proposal

    @staticmethod
    def _requires_reconciliation(proposal: Proposal | None) -> bool:
        if proposal is None or proposal.vote_message_id is not None:
            return False
        if proposal.status == "failed":
            return proposal.publication_error_code not in {
                "send_rejected",
                "send_failed",
            }
        if proposal.status != "publishing":
            return False
        started_at = proposal.publication_started_at
        if started_at is None:
            return True
        now = datetime.now().astimezone()
        normalized_started_at = started_at.astimezone()
        return normalized_started_at <= now - timedelta(
            seconds=PUBLICATION_LEASE_SECONDS
        )
