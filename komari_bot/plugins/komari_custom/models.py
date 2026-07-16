"""komari_custom 插件数据模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from komari_bot.common.content_budget import (
    PROPOSAL_CONTENT_TEXT_BUDGET,
    TITLE_TEXT_BUDGET,
    normalize_required_text,
)

ProposalStatus = Literal["publishing", "failed", "voting", "approving", "approved"]
SessionPhase = Literal["title", "content", "review"]
UndoAction = Literal["append", "replace", "delete"]


class Proposal(BaseModel):
    """知识库提案记录。"""

    id: int
    group_id: int
    proposer_id: int
    proposer_name: str | None = None
    title: str
    content: str
    status: ProposalStatus
    publication_key: str
    publication_token: str | None = None
    publication_started_at: datetime | None = None
    publication_attempts: int = 0
    publication_error_code: str | None = None
    vote_message_id: int | None = None
    vote_count: int = 0
    required_votes: int
    voted_users: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    approved_at: datetime | None = None
    knowledge_id: int | None = None
    expired_at: datetime | None = None
    approval_token: str | None = None
    approval_started_at: datetime | None = None


class UndoRecord(BaseModel):
    """编辑撤销记录。"""

    action: UndoAction
    field: Literal["title", "content"]
    text: str | None = None
    old: str | None = None
    new: str | None = None


class SessionData(BaseModel):
    """Redis 中保存的多步编辑会话。"""

    model_config = ConfigDict(validate_assignment=True)

    phase: SessionPhase = "title"
    title: str = ""
    content: str = ""
    undo_stack: list[UndoRecord] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now().astimezone().isoformat())
    publication_message_id: int | None = None

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        """允许空草稿，否则执行标题预算。"""
        if not value.strip():
            return ""
        return normalize_required_text(
            value,
            label="提案标题",
            budget=TITLE_TEXT_BUDGET,
        )

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        """允许空草稿，否则执行正文预算。"""
        if not value.strip():
            return ""
        return normalize_required_text(
            value,
            label="提案正文",
            budget=PROPOSAL_CONTENT_TEXT_BUDGET,
        )

    def current_field(self) -> Literal["title", "content"]:
        """返回当前可编辑字段。"""
        return "title" if self.phase == "title" else "content"
