"""Komari Memory 管理 API 数据模型。"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003
from typing import Any

from pydantic import BaseModel, Field, field_validator


class ConversationEntry(BaseModel):
    """对话记忆条目。"""

    id: int
    group_id: str
    summary: str
    participants: list[str] = Field(default_factory=list)
    start_time: datetime
    end_time: datetime
    importance_initial: int
    importance_current: int
    last_accessed: datetime | None = None
    created_at: datetime | None = None


class ConversationListResponse(BaseModel):
    """对话记忆列表响应。"""

    items: list[ConversationEntry]
    total: int
    limit: int
    offset: int


class ConversationCreateRequest(BaseModel):
    """创建对话记忆请求。"""

    group_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    participants: list[str] = Field(default_factory=list)
    importance_initial: int = Field(default=3, ge=1, le=5)
    importance_current: int | None = Field(default=None, ge=0, le=5)
    start_time: datetime | None = None
    end_time: datetime | None = None
    last_accessed: datetime | None = None

    @field_validator("group_id", "summary")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("字段不能为空")
        return stripped

    @field_validator("participants")
    @classmethod
    def normalize_participants(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item.strip()]


class ConversationUpdateRequest(BaseModel):
    """更新对话记忆请求。"""

    group_id: str | None = Field(default=None, min_length=1)
    summary: str | None = Field(default=None, min_length=1)
    participants: list[str] | None = None
    importance_initial: int | None = Field(default=None, ge=1, le=5)
    importance_current: int | None = Field(default=None, ge=0, le=5)
    start_time: datetime | None = None
    end_time: datetime | None = None
    last_accessed: datetime | None = None

    @field_validator("group_id", "summary")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("字段不能为空")
        return stripped

    @field_validator("participants")
    @classmethod
    def normalize_optional_participants(
        cls,
        value: list[str] | None,
    ) -> list[str] | None:
        if value is None:
            return None
        return [item.strip() for item in value if item.strip()]


class MemoryEntityEntry(BaseModel):
    """实体文档条目。"""

    user_id: str
    group_id: str
    key: str
    category: str
    importance: int
    access_count: int
    last_accessed: datetime | None = None
    value: dict[str, Any]


class MemoryEntityListResponse(BaseModel):
    """实体文档列表响应。"""

    items: list[MemoryEntityEntry]
    total: int
    limit: int
    offset: int
