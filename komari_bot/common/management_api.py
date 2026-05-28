"""本地管理 API 共享辅助工具。"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette import status


@dataclass(frozen=True, slots=True)
class SharedManagementSettings:
    """共享管理 API 配置。"""

    api_token: str
    allowed_origins: tuple[str, ...]


def create_bearer_auth_dependency(
    api_token: str,
    *,
    detail: str = "未授权访问管理接口",
) -> Callable[[HTTPAuthorizationCredentials | None], Any]:
    """创建 Bearer Token 鉴权依赖。"""
    bearer_scheme = HTTPBearer(auto_error=False)

    async def _verify_token(
        authorization: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    ) -> None:
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
        if not secrets.compare_digest(token, api_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=detail,
            )

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
    api_token = getattr(config, "api_token", "")
    if not isinstance(api_token, str) or not api_token.strip():
        logger.warning(f"{warning_prefix} 未配置 api_token，跳过管理 API 注册")
        return None

    return SharedManagementSettings(
        api_token=api_token.strip(),
        allowed_origins=normalize_origins(getattr(config, "api_allowed_origins", [])),
    )
