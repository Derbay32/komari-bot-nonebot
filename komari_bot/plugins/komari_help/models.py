"""Komari Help 共享数据模型。"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from komari_bot.common.content_budget import (
    CONTENT_TEXT_BUDGET,
    IDENTIFIER_TEXT_BUDGET,
    NOTES_TEXT_BUDGET,
    QUERY_TEXT_BUDGET,
    TITLE_TEXT_BUDGET,
    normalize_keywords,
    normalize_optional_text,
    normalize_required_text,
)

HelpCategory = Literal["command", "feature", "faq", "other"]
HelpSource = Literal["keyword", "vector"]


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

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        return normalize_required_text(
            value,
            label="帮助标题",
            budget=TITLE_TEXT_BUDGET,
        )

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        return normalize_required_text(
            value,
            label="帮助内容",
            budget=CONTENT_TEXT_BUDGET,
        )

    @field_validator("keywords")
    @classmethod
    def validate_keywords(cls, value: list[str]) -> list[str]:
        return normalize_keywords(value, require_nonempty=False)

    @field_validator("plugin_name")
    @classmethod
    def normalize_plugin_name(cls, value: str | None) -> str | None:
        return normalize_optional_text(
            value,
            label="插件名",
            budget=IDENTIFIER_TEXT_BUDGET,
        )

    @field_validator("notes")
    @classmethod
    def normalize_notes(cls, value: str | None) -> str | None:
        return normalize_optional_text(
            value,
            label="备注",
            budget=NOTES_TEXT_BUDGET,
        )


class HelpUpdateRequest(BaseModel):
    """更新帮助条目请求。"""

    title: str | None = None
    content: str | None = None
    keywords: list[str] | None = None
    category: HelpCategory | None = None
    plugin_name: str | None = None
    notes: str | None = None

    @field_validator("title")
    @classmethod
    def validate_optional_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_required_text(
            value,
            label="帮助标题",
            budget=TITLE_TEXT_BUDGET,
        )

    @field_validator("content")
    @classmethod
    def validate_optional_content(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_required_text(
            value,
            label="帮助内容",
            budget=CONTENT_TEXT_BUDGET,
        )

    @field_validator("keywords")
    @classmethod
    def validate_optional_keywords(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return normalize_keywords(value, require_nonempty=False)

    @field_validator("plugin_name")
    @classmethod
    def normalize_optional_plugin_name(cls, value: str | None) -> str | None:
        return normalize_optional_text(
            value,
            label="插件名",
            budget=IDENTIFIER_TEXT_BUDGET,
        )

    @field_validator("notes")
    @classmethod
    def normalize_optional_notes(cls, value: str | None) -> str | None:
        return normalize_optional_text(
            value,
            label="备注",
            budget=NOTES_TEXT_BUDGET,
        )


class HelpSearchRequest(BaseModel):
    """帮助检索请求。"""

    query: str
    limit: int = Field(default=5, ge=1, le=20)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        return normalize_required_text(
            value,
            label="查询文本",
            budget=QUERY_TEXT_BUDGET,
        )


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
