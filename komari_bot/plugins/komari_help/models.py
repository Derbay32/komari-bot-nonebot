"""Komari Help 共享数据模型。"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003
from typing import Literal

from pydantic import BaseModel, Field, field_validator

HelpCategory = Literal["command", "feature", "faq", "other"]
HelpSource = Literal["keyword", "vector"]


def _normalize_keywords(value: list[str]) -> list[str]:
    """清理关键词中的空白条目与重复值。"""
    normalized: list[str] = []
    seen: set[str] = set()
    for keyword in value:
        cleaned = keyword.strip()
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        normalized.append(cleaned)
    return normalized


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


class HelpEntry(BaseModel):
    """帮助文档单条记录。"""

    id: int
    category: HelpCategory
    plugin_name: str | None = None
    keywords: list[str] = Field(default_factory=list)
    title: str
    content: str
    notes: str | None = None
    is_auto_generated: bool = False
    created_at: datetime
    updated_at: datetime


class HelpListResponse(BaseModel):
    """帮助条目列表响应。"""

    items: list[HelpEntry]
    total: int
    limit: int
    offset: int


class HelpCreateRequest(BaseModel):
    """新增帮助条目请求。"""

    title: str
    content: str
    keywords: list[str] = Field(default_factory=list)
    category: HelpCategory = "other"
    plugin_name: str | None = None
    notes: str | None = None

    @field_validator("title", "content")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("字段不能为空")
        return normalized

    @field_validator("keywords")
    @classmethod
    def validate_keywords(cls, value: list[str]) -> list[str]:
        return _normalize_keywords(value)

    @field_validator("plugin_name", "notes")
    @classmethod
    def normalize_optional_text_fields(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)


class HelpUpdateRequest(BaseModel):
    """更新帮助条目请求。"""

    title: str | None = None
    content: str | None = None
    keywords: list[str] | None = None
    category: HelpCategory | None = None
    plugin_name: str | None = None
    notes: str | None = None

    @field_validator("title", "content")
    @classmethod
    def validate_optional_required_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("字段不能为空")
        return normalized

    @field_validator("keywords")
    @classmethod
    def validate_optional_keywords(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return _normalize_keywords(value)

    @field_validator("plugin_name", "notes")
    @classmethod
    def normalize_optional_text_fields(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)


class HelpSearchRequest(BaseModel):
    """帮助检索请求。"""

    query: str
    limit: int = Field(default=5, ge=1, le=20)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("查询文本不能为空")
        return normalized


class HelpSearchResult(BaseModel):
    """帮助检索结果。"""

    id: int
    category: HelpCategory
    plugin_name: str | None = None
    title: str
    content: str
    similarity: float = 0.0
    source: HelpSource = "keyword"


class HelpScanResponse(BaseModel):
    """扫描结果响应。"""

    updated_count: int
