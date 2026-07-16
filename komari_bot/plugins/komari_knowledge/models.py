"""Komari Knowledge 共享数据模型。"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from komari_bot.common.content_budget import (
    CONTENT_TEXT_BUDGET,
    NOTES_TEXT_BUDGET,
    QUERY_TEXT_BUDGET,
    normalize_keywords,
    normalize_optional_text,
    normalize_required_text,
)

KnowledgeCategory = Literal["general", "character", "setting", "plot", "other", "custom"]
KnowledgeSource = Literal["keyword", "vector"]


class KnowledgeEntry(BaseModel):
    """知识库单条记录。"""

    id: int
    category: KnowledgeCategory
    keywords: list[str] = Field(default_factory=list)
    content: str
    notes: str | None = None
    created_at: datetime
    updated_at: datetime


class KnowledgeListResponse(BaseModel):
    """知识列表响应。"""

    items: list[KnowledgeEntry]
    total: int
    limit: int
    offset: int


class KnowledgeCreateRequest(BaseModel):
    """新增知识请求。"""

    content: str
    keywords: list[str]
    category: KnowledgeCategory = "general"
    notes: str | None = None

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        """清理内容并执行统一预算。"""
        return normalize_required_text(
            value,
            label="知识内容",
            budget=CONTENT_TEXT_BUDGET,
        )

    @field_validator("keywords")
    @classmethod
    def validate_keywords(cls, value: list[str]) -> list[str]:
        """清理关键词并执行统一预算。"""
        return normalize_keywords(value, require_nonempty=True)

    @field_validator("notes")
    @classmethod
    def normalize_notes(cls, value: str | None) -> str | None:
        """把空备注归一为 None。"""
        return normalize_optional_text(
            value,
            label="备注",
            budget=NOTES_TEXT_BUDGET,
        )


class KnowledgeUpdateRequest(BaseModel):
    """更新知识请求。"""

    content: str | None = None
    keywords: list[str] | None = None
    category: KnowledgeCategory | None = None
    notes: str | None = None

    @field_validator("content")
    @classmethod
    def validate_optional_content(cls, value: str | None) -> str | None:
        """拒绝仅包含空白的内容。"""
        if value is None:
            return None
        return normalize_required_text(
            value,
            label="知识内容",
            budget=CONTENT_TEXT_BUDGET,
        )

    @field_validator("keywords")
    @classmethod
    def validate_optional_keywords(cls, value: list[str] | None) -> list[str] | None:
        """拒绝显式传入空关键词列表。"""
        if value is None:
            return None
        return normalize_keywords(value, require_nonempty=True)

    @field_validator("notes")
    @classmethod
    def normalize_optional_notes(cls, value: str | None) -> str | None:
        """把空字符串备注归一为 None。"""
        return normalize_optional_text(
            value,
            label="备注",
            budget=NOTES_TEXT_BUDGET,
        )


class KnowledgeSearchRequest(BaseModel):
    """检索测试请求。"""

    query: str
    limit: int = Field(default=5, ge=1, le=20)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        """拒绝空查询。"""
        return normalize_required_text(
            value,
            label="查询文本",
            budget=QUERY_TEXT_BUDGET,
        )


class KnowledgeSearchHit(BaseModel):
    """检索结果。"""

    id: int
    category: KnowledgeCategory
    content: str
    similarity: float = 0.0
    source: KnowledgeSource = "keyword"


SearchResult = KnowledgeSearchHit
