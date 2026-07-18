"""Komari Management 配置 Schema。"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from komari_bot.common.management_api import management_token_meets_minimum_strength

DEFAULT_ANNOUNCE_STATUS_PAGE_URL = "https://your.status.page/url/here"
_CREDENTIAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_PERMISSION_PATTERN = re.compile(r"^(?:\*|[a-z][a-z0-9_-]*:(?:\*|[a-z][a-z0-9_-]*))$")
_TOKEN_PATTERN = re.compile(r"^[!-~]+$")


class ManagementCredentialSchema(BaseModel):
    """一条具名、可限权和可定时撤销的管理凭据。"""

    model_config = ConfigDict(extra="forbid")

    credential_id: str = Field(
        min_length=1,
        max_length=64,
        description="稳定的管理操作者或自动化凭据 ID",
    )
    token: str = Field(
        min_length=16,
        max_length=512,
        description="Bearer Token 正文",
        json_schema_extra={"secret": True},
    )
    permissions: list[str] = Field(
        min_length=1,
        max_length=64,
        description="权限范围，例如 config:read、config:write、announce:send 或 *",
    )
    revoked_at: datetime | None = Field(
        default=None,
        description="到达该时间后自动撤销；空值表示持续有效",
    )

    @field_validator("credential_id")
    @classmethod
    def validate_credential_id(cls, value: str) -> str:
        normalized = value.strip()
        if not _CREDENTIAL_ID_PATTERN.fullmatch(normalized):
            msg = "credential_id 只能包含字母、数字、点、下划线和短横线"
            raise ValueError(msg)
        return normalized

    @field_validator("token")
    @classmethod
    def normalize_token(cls, value: str) -> str:
        normalized = value.strip()
        if not management_token_meets_minimum_strength(normalized):
            msg = "管理凭据 Token 至少 16 字符，并需要不少于 8 个不同的可打印 ASCII 字符"
            raise ValueError(msg)
        if not _TOKEN_PATTERN.fullmatch(normalized):
            msg = "管理凭据 Token 只能包含不带空格的 ASCII 可打印字符"
            raise ValueError(msg)
        return normalized

    @field_validator("permissions")
    @classmethod
    def normalize_permissions(cls, value: list[str]) -> list[str]:
        permissions: list[str] = []
        for item in value:
            permission = item.strip().lower()
            if not _PERMISSION_PATTERN.fullmatch(permission):
                msg = f"无效的管理权限范围: {permission}"
                raise ValueError(msg)
            if permission not in permissions:
                permissions.append(permission)
        if not permissions:
            msg = "管理凭据至少需要一项权限"
            raise ValueError(msg)
        return permissions

    @field_validator("revoked_at")
    @classmethod
    def normalize_revoked_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "revoked_at 必须包含明确时区"
            raise ValueError(msg)
        return value.astimezone(UTC)


class DynamicConfigSchema(BaseModel):
    """Komari Management 配置模型。"""

    model_config = ConfigDict(
        json_schema_extra={"default_apply_mode": "restart"},
    )

    version: str = Field(default="1.0", description="配置架构版本")
    last_updated: str = Field(
        default_factory=lambda: datetime.now().astimezone().isoformat(),
        description="最后更新时间戳",
    )

    plugin_enable: bool = Field(default=False, description="是否启用统一管理 API 插件")
    api_token: str = Field(
        default="",
        description="旧版单一管理 API Bearer Token；api_credentials 非空时不再接受",
        json_schema_extra={"secret": True, "apply_mode": "immediate"},
    )
    api_credentials: list[ManagementCredentialSchema] = Field(
        default_factory=list,
        max_length=32,
        description="具名管理凭据；非空时完全替代旧版 api_token",
        json_schema_extra={"secret": True, "apply_mode": "immediate"},
    )
    api_allowed_origins: list[str] = Field(
        default_factory=list,
        description="允许访问管理 API 的前端 Origin 白名单",
    )
    announce_status_page_url: str = Field(
        default=DEFAULT_ANNOUNCE_STATUS_PAGE_URL,
        description="维护通知中使用的状态页面链接",
    )
    announce_max_group_count: int = Field(
        default=20,
        ge=1,
        le=100,
        description="单次维护通知最多发送的群数量",
    )
    announce_send_interval_seconds: float = Field(
        default=1.0,
        ge=0.0,
        le=60.0,
        description="维护通知逐群发送间隔秒数",
    )
    announce_request_cooldown_seconds: float = Field(
        default=30.0,
        ge=0.0,
        le=3600.0,
        description="维护通知请求级冷却秒数",
    )

    @field_validator("api_allowed_origins", mode="before")
    @classmethod
    def parse_list_string(cls, value: Any) -> Any:
        """兼容从 .env 读取字符串列表。"""
        if isinstance(value, str):
            import json

            try:
                parsed = json.loads(value)
                return [str(item) for item in parsed]
            except (json.JSONDecodeError, TypeError):
                return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("announce_status_page_url")
    @classmethod
    def validate_announce_status_page_url(cls, value: str) -> str:
        """校验维护通知状态页链接。"""
        normalized = value.strip()
        if not normalized:
            msg = "announce_status_page_url 不能为空"
            raise ValueError(msg)
        return normalized

    @model_validator(mode="after")
    def validate_unique_credentials(self) -> DynamicConfigSchema:
        credential_ids = [item.credential_id for item in self.api_credentials]
        if len(credential_ids) != len(set(credential_ids)):
            msg = "api_credentials 的 credential_id 不允许重复"
            raise ValueError(msg)
        tokens = [item.token for item in self.api_credentials]
        if len(tokens) != len(set(tokens)):
            msg = "api_credentials 不允许复用同一个 Token"
            raise ValueError(msg)
        return self
