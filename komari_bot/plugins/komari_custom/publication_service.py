"""知识提案发布状态机与故障恢复。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from nonebot import logger

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
            raise ProposalPublicationInProgressError("publication_in_progress")

        try:
            sent = await send_message(claimed)
        except Exception as exc:
            await self._repository.mark_publication_failed(
                claimed.id,
                publication_token,
                "send_failed",
            )
            raise ProposalPublicationError("send_failed") from exc

        message_id = extract_message_id(sent)
        if message_id is None:
            await self._repository.mark_publication_failed(
                claimed.id,
                publication_token,
                "message_id_missing",
            )
            raise ProposalPublicationError("message_id_missing")

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
