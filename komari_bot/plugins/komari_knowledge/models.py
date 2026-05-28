"""Komari Knowledge 共享数据模型。"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003
from typing import Literal

from pydantic import BaseModel, Field, field_validator

KnowledgeCategory = Literal["general", "character", "setting", "plot", "other"]
KnowledgeSource = Literal["keyword", "vector"]


def _normalize_keywords(value: list[str]) -> list[str]:
    """清理关键词中的空白条目。"""
    normalized = [keyword.strip() for keyword in value if keyword.strip()]
    if not normalized:
        raise ValueError("关键词不能为空")
    return normalized


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
        """拒绝空内容。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("知识内容不能为空")
        return normalized

    @field_validator("keywords")
    @classmethod
    def validate_keywords(cls, value: list[str]) -> list[str]:
        """拒绝空关键词列表。"""
        return _normalize_keywords(value)

    @field_validator("notes")
    @classmethod
    def normalize_notes(cls, value: str | None) -> str | None:
        """把空备注归一为 None。"""
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


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
        normalized = value.strip()
        if not normalized:
            raise ValueError("知识内容不能为空")
        return normalized

    @field_validator("keywords")
    @classmethod
    def validate_optional_keywords(cls, value: list[str] | None) -> list[str] | None:
        """拒绝显式传入空关键词列表。"""
        if value is None:
            return None
        return _normalize_keywords(value)

    @field_validator("notes")
    @classmethod
    def normalize_optional_notes(cls, value: str | None) -> str | None:
        """把空字符串备注归一为 None。"""
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class KnowledgeSearchRequest(BaseModel):
    """检索测试请求。"""

    query: str
    limit: int = Field(default=5, ge=1, le=20)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        """拒绝空查询。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("查询文本不能为空")
        return normalized


class KnowledgeSearchHit(BaseModel):
    """检索结果。"""

    id: int
    category: KnowledgeCategory
    content: str
    similarity: float = 0.0
    source: KnowledgeSource = "keyword"


SearchResult = KnowledgeSearchHit
