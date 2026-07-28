"""本地管理 API 共享辅助工具。"""

from __future__ import annotations

import re
import secrets
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from inspect import isawaitable
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette import status

type ManagementCredentialSourceValue = str | Sequence[object]
type ManagementTokenSource = (
    ManagementCredentialSourceValue
    | Callable[
        [],
        ManagementCredentialSourceValue | Awaitable[ManagementCredentialSourceValue],
    ]
)

_CREDENTIAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_PERMISSION_PATTERN = re.compile(
    r"^(?:\*|[a-z][a-z0-9_-]*:(?:\*|[a-z][a-z0-9_-]*))$"
)
_READ_PERMISSION_IMPLICATIONS: Mapping[str, frozenset[str]] = {
    "announce:read": frozenset({"announce:send"}),
    "config:read": frozenset({"config:write"}),
    "help:read": frozenset({"help:write"}),
    "knowledge:read": frozenset({"knowledge:write"}),
    "memory:read": frozenset({"memory:write"}),
    "prompt:read": frozenset({"prompt:write"}),
    "scene:read": frozenset({"scene:write"}),
    "user_ban:read": frozenset({"user_ban:write"}),
}
LEGACY_TOKEN_REMOVAL_VERSION = "2.0"
MIN_MANAGEMENT_TOKEN_LENGTH = 16
MIN_MANAGEMENT_TOKEN_UNIQUE_CHARACTERS = 8


@dataclass(frozen=True, slots=True)
class ManagementCredential:
    """规范化后的管理凭据。"""

    credential_id: str
    token: str
    permissions: frozenset[str]
    revoked_at: datetime | None = None

    def is_active(self, now: datetime | None = None) -> bool:
        """判断凭据是否尚未到撤销时间。"""
        if self.revoked_at is None:
            return True
        current = now or datetime.now(tz=UTC)
        revoked_at = self.revoked_at
        if revoked_at.tzinfo is None:
            revoked_at = revoked_at.replace(tzinfo=UTC)
        return current < revoked_at.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ManagementPrincipal:
    """通过 Bearer 鉴权后的管理操作者身份。"""

    operator_id: str
    permissions: frozenset[str]

    def has_permission(self, required_permission: str) -> bool:
        """检查精确权限、资源通配符及显式声明的权限蕴含。"""
        if "*" in self.permissions or required_permission in self.permissions:
            return True
        resource, separator, action = required_permission.partition(":")
        if not separator:
            return False
        if f"{resource}:*" in self.permissions:
            return True
        if action != "read":
            return False
        return bool(
            self.permissions
            & _READ_PERMISSION_IMPLICATIONS.get(
                required_permission,
                frozenset(),
            )
        )


@dataclass(frozen=True, slots=True)
class SharedManagementSettings:
    """共享管理 API 配置。"""

    credential_source: ManagementCredentialSourceValue
    allowed_origins: tuple[str, ...]


def _legacy_credential(token: str) -> ManagementCredential:
    return ManagementCredential(
        credential_id="legacy-api-token",
        token=token,
        permissions=frozenset({"*"}),
    )


def management_token_meets_minimum_strength(token: str) -> bool:
    """校验管理 Token 的长度、字符集和最低字符多样性。"""
    return (
        MIN_MANAGEMENT_TOKEN_LENGTH <= len(token) <= 512
        and all(0x21 <= ord(character) <= 0x7E for character in token)
        and len(set(token)) >= MIN_MANAGEMENT_TOKEN_UNIQUE_CHARACTERS
    )


def _read_credential_value(raw: object, field_name: str) -> object:
    if isinstance(raw, Mapping):
        return raw.get(field_name)
    return getattr(raw, field_name, None)


def _parse_revoked_at(raw_value: object) -> datetime | None:
    if raw_value is None or raw_value == "":
        return None
    if isinstance(raw_value, datetime):
        return raw_value
    try:
        return datetime.fromisoformat(str(raw_value))
    except ValueError:
        return datetime.now(tz=UTC)


def _normalize_permission_set(raw_value: object) -> frozenset[str] | None:
    if not isinstance(raw_value, Sequence) or isinstance(raw_value, str):
        return None
    permissions = [str(item).strip().lower() for item in raw_value]
    if not permissions or any(
        not permission or not _PERMISSION_PATTERN.fullmatch(permission)
        for permission in permissions
    ):
        return None
    return frozenset(permissions)


def _normalize_credentials(
    source: ManagementCredentialSourceValue,
) -> tuple[ManagementCredential, ...]:
    """把旧单 Token 或多凭据配置统一成只读凭据快照。"""
    if isinstance(source, str):
        token = source.strip()
        return (
            (_legacy_credential(token),)
            if management_token_meets_minimum_strength(token)
            else ()
        )
    if not isinstance(source, Sequence):
        return ()

    credentials: list[ManagementCredential] = []
    for raw in source:
        credential_id = str(_read_credential_value(raw, "credential_id") or "").strip()
        token = str(_read_credential_value(raw, "token") or "").strip()
        permissions = _normalize_permission_set(
            _read_credential_value(raw, "permissions")
        )
        if (
            not _CREDENTIAL_ID_PATTERN.fullmatch(credential_id)
            or not management_token_meets_minimum_strength(token)
        ):
            continue
        if permissions is None:
            continue
        credentials.append(
            ManagementCredential(
                credential_id=credential_id,
                token=token,
                permissions=permissions,
                revoked_at=_parse_revoked_at(
                    _read_credential_value(raw, "revoked_at")
                ),
            )
        )
    return tuple(credentials)


async def _resolve_credential_source(
    source: ManagementTokenSource,
) -> tuple[ManagementCredential, ...]:
    current_source = source() if callable(source) else source
    if isawaitable(current_source):
        current_source = await current_source
    return _normalize_credentials(current_source)


def create_bearer_auth_dependency(
    api_token: ManagementTokenSource,
    *,
    detail: str = "未授权访问管理接口",
    required_permission: str | None = None,
) -> Callable[..., Awaitable[ManagementPrincipal]]:
    """创建 Bearer Token 鉴权依赖。"""
    bearer_scheme = HTTPBearer(auto_error=False)

    async def _verify_token(
        request: Request,
        authorization: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    ) -> ManagementPrincipal:
        if authorization is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=detail,
            )

        scheme = authorization.scheme
        token = authorization.credentials
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=detail,
            )
        matched_credential: ManagementCredential | None = None
        for credential in await _resolve_credential_source(api_token):
            token_matches = secrets.compare_digest(
                token.encode("utf-8"),
                credential.token.encode("utf-8"),
            )
            if token_matches and credential.is_active():
                matched_credential = credential
        if matched_credential is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=detail,
            )

        principal = ManagementPrincipal(
            operator_id=matched_credential.credential_id,
            permissions=matched_credential.permissions,
        )
        if required_permission and not principal.has_permission(required_permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="当前管理凭据没有所需权限",
            )
        request.state.management_principal = principal
        return principal

    return _verify_token


def normalize_origins(raw_value: Any) -> tuple[str, ...]:
    """归一化并去重 Origin 白名单。"""
    values: list[str]
    if isinstance(raw_value, str):
        values = [item.strip() for item in raw_value.split(",")]
    elif isinstance(raw_value, Sequence):
        values = [str(item).strip() for item in raw_value]
    else:
        values = []

    unique_values: list[str] = []
    for value in values:
        if value and value not in unique_values:
            unique_values.append(value)
    return tuple(unique_values)


def ensure_management_cors(app: FastAPI, allowed_origins: Sequence[str]) -> None:
    """为本地管理 API 挂载一次共享 CORS 中间件。"""
    origins = normalize_origins(list(allowed_origins))
    if not origins:
        return

    registered = tuple(getattr(app.state, "komari_management_cors_origins", ()))
    if registered:
        if set(registered) != set(origins):
            msg = "管理 API 的 CORS 白名单配置不一致"
            raise RuntimeError(msg)
        return

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(origins),
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.komari_management_cors_origins = origins


def resolve_management_settings(
    config: Any,
    *,
    logger: Any,
    warning_prefix: str = "[Komari Management]",
) -> SharedManagementSettings | None:
    """从配置对象解析管理 API 共用设置。"""
    raw_credentials = getattr(config, "api_credentials", [])
    credential_source: ManagementCredentialSourceValue
    if isinstance(raw_credentials, Sequence) and not isinstance(raw_credentials, str):
        credential_source = raw_credentials
    else:
        credential_source = []
    if not credential_source:
        api_token = getattr(config, "api_token", "")
        credential_source = api_token if isinstance(api_token, str) else ""

    if not _normalize_credentials(credential_source):
        if isinstance(credential_source, str) and credential_source.strip():
            logger.warning(
                f"{warning_prefix} 旧版 api_token 不符合最低强度要求，"
                "管理 API 已拒绝启动；请改用至少 16 字符且字符足够多样的具名凭据"
            )
        else:
            logger.warning(f"{warning_prefix} 未配置有效管理凭据，跳过管理 API 注册")
        return None

    if isinstance(credential_source, str):
        logger.warning(
            f"{warning_prefix} 旧版 api_token 已弃用，将在 "
            f"v{LEGACY_TOKEN_REMOVAL_VERSION} 移除；请迁移到 api_credentials"
        )

    return SharedManagementSettings(
        credential_source=credential_source,
        allowed_origins=normalize_origins(getattr(config, "api_allowed_origins", [])),
    )
