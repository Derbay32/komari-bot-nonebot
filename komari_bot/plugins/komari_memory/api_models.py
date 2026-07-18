"""Komari Memory 管理 API 数据模型。"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003
from typing import Any

from pydantic import BaseModel, Field, RootModel, field_validator

from komari_bot.common.content_budget import (
    CONTENT_TEXT_BUDGET,
    IDENTIFIER_TEXT_BUDGET,
    KEYWORD_TEXT_BUDGET,
    TITLE_TEXT_BUDGET,
    ContentValidationError,
    normalize_identifiers,
    normalize_required_text,
    validate_json_budget,
)

MAX_PROFILE_TRAIT_COUNT = 100


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


class ConversationDeadLetterEntry(BaseModel):
    """不含消息正文的对话总结失败快照摘要。"""

    group_id: str
    snapshot_id: str
    failure_code: str
    attempt_count: int
    failed_at_ms: int
    message_count: int
    chunk_state_count: int


class ConversationDeadLetterListResponse(BaseModel):
    """对话总结失败快照列表响应。"""

    items: list[ConversationDeadLetterEntry]
    limit: int


class ConversationDeadLetterRequeueResponse(BaseModel):
    """失败快照重新入队响应。"""

    group_id: str
    snapshot_id: str
    restored_message_count: int


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

    @field_validator("group_id")
    @classmethod
    def normalize_group_id(cls, value: str) -> str:
        return normalize_required_text(
            value,
            label="群组 ID",
            budget=IDENTIFIER_TEXT_BUDGET,
        )

    @field_validator("summary")
    @classmethod
    def normalize_summary(cls, value: str) -> str:
        return normalize_required_text(
            value,
            label="对话摘要",
            budget=CONTENT_TEXT_BUDGET,
        )

    @field_validator("participants")
    @classmethod
    def normalize_participants(cls, value: list[str]) -> list[str]:
        return normalize_identifiers(
            value,
            label="参与者",
            require_nonempty=False,
        )


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

    @field_validator("group_id")
    @classmethod
    def normalize_optional_group_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_required_text(
            value,
            label="群组 ID",
            budget=IDENTIFIER_TEXT_BUDGET,
        )

    @field_validator("summary")
    @classmethod
    def normalize_optional_summary(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_required_text(
            value,
            label="对话摘要",
            budget=CONTENT_TEXT_BUDGET,
        )

    @field_validator("participants")
    @classmethod
    def normalize_optional_participants(
        cls,
        value: list[str] | None,
    ) -> list[str] | None:
        if value is None:
            return None
        return normalize_identifiers(
            value,
            label="参与者",
            require_nonempty=False,
        )


class UserProfileUpsertRequest(RootModel[dict[str, Any]]):
    """有结构与内容预算的用户画像写入载荷。"""

    @field_validator("root")
    @classmethod
    def normalize_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        payload = dict(value)

        if "user_id" in payload:
            raw_user_id = payload["user_id"]
            if not isinstance(raw_user_id, str):
                message = "用户 ID 必须是字符串"
                raise ContentValidationError(message)
            payload["user_id"] = normalize_required_text(
                raw_user_id,
                label="用户 ID",
                budget=IDENTIFIER_TEXT_BUDGET,
            )

        if "display_name" in payload:
            raw_display_name = payload["display_name"]
            if not isinstance(raw_display_name, str):
                message = "显示名称必须是字符串"
                raise ContentValidationError(message)
            payload["display_name"] = normalize_required_text(
                raw_display_name,
                label="显示名称",
                budget=TITLE_TEXT_BUDGET,
            )

        if "traits" in payload:
            raw_traits = payload["traits"]
            if not isinstance(raw_traits, dict):
                message = "traits 必须是 JSON 对象"
                raise ContentValidationError(message)
            if len(raw_traits) > MAX_PROFILE_TRAIT_COUNT:
                message = (
                    "画像 trait 数量超过上限"
                    f"（当前 {len(raw_traits)}，最多 {MAX_PROFILE_TRAIT_COUNT}）"
                )
                raise ContentValidationError(message)
            normalized_traits: dict[str, dict[str, Any]] = {}
            seen: set[str] = set()
            for raw_key, raw_trait in raw_traits.items():
                key = normalize_required_text(
                    raw_key,
                    label="画像 trait 名称",
                    budget=KEYWORD_TEXT_BUDGET,
                )
                folded_key = key.casefold()
                if folded_key in seen:
                    message = "画像 trait 名称清理后重复"
                    raise ContentValidationError(message)
                if not isinstance(raw_trait, dict):
                    message = "画像 trait 值必须是 JSON 对象"
                    raise ContentValidationError(message)
                seen.add(folded_key)
                normalized_traits[key] = dict(raw_trait)
            payload["traits"] = normalized_traits

        validate_json_budget(payload, label="用户画像 JSON")
        return payload


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


class InteractionEventEntry(BaseModel):
    """跨群互动事件记忆条目。"""

    id: int
    user_id: str
    display_name: str
    event_summary: str
    source_message_count: int
    first_seen_at: datetime
    last_seen_at: datetime
    importance: int
    importance_initial: int
    importance_current: int
    last_accessed: datetime | None = None
    is_fuzzy: bool = False
    created_at: datetime | None = None


class InteractionEventListResponse(BaseModel):
    """跨群互动事件记忆列表响应。"""

    items: list[InteractionEventEntry]
    total: int
    limit: int
    offset: int


class InteractionEventUpdateRequest(BaseModel):
    """更新跨群互动事件请求。"""

    event_summary: str | None = Field(default=None, min_length=1)
    importance_initial: int | None = Field(default=None, ge=1, le=5)
    importance_current: int | None = Field(default=None, ge=0, le=5)

    @field_validator("event_summary")
    @classmethod
    def normalize_optional_summary(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_required_text(
            value,
            label="互动事件摘要",
            budget=CONTENT_TEXT_BUDGET,
        )
