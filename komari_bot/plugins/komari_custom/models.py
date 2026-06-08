"""komari_custom 插件数据模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

ProposalStatus = Literal["voting", "approved"]
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
    vote_message_id: int | None = None
    vote_count: int = 0
    required_votes: int
    voted_users: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    approved_at: datetime | None = None
    knowledge_id: int | None = None
    expired_at: datetime | None = None


class UndoRecord(BaseModel):
    """编辑撤销记录。"""

    action: UndoAction
    field: Literal["title", "content"]
    text: str | None = None
    old: str | None = None
    new: str | None = None


class SessionData(BaseModel):
    """Redis 中保存的多步编辑会话。"""

    phase: SessionPhase = "title"
    title: str = ""
    content: str = ""
    undo_stack: list[UndoRecord] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now().astimezone().isoformat())

    @field_validator("title", "content")
    @classmethod
    def trim_surrounding_blank(cls, value: str) -> str:
        """去掉首尾空白，保留正文内部换行。"""
        return value.strip()

    def current_field(self) -> Literal["title", "content"]:
        """返回当前可编辑字段。"""
        return "title" if self.phase == "title" else "content"
