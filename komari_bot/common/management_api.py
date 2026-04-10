"""本地管理 API 共享辅助工具。"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from nonebot.plugin import require
from starlette import status

from komari_bot.plugins.komari_knowledge.config_schema import (
    DynamicConfigSchema as KnowledgeConfigSchema,
)

config_manager_plugin = require("config_manager")


@dataclass(frozen=True, slots=True)
class SharedManagementSettings:
    """共享管理 API 配置。"""

    api_token: str
    allowed_origins: tuple[str, ...]


def create_bearer_auth_dependency(
    api_token: str,
    *,
    detail: str = "未授权访问管理接口",
) -> Callable[[str | None], Any]:
    """创建 Bearer Token 鉴权依赖。"""

    async def _verify_token(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        if authorization is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=detail,
            )

        scheme, _, token = authorization.partition(" ")
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


def load_shared_management_settings(logger: Any) -> SharedManagementSettings | None:
    """从 komari_knowledge 配置读取共享管理 API 凭证。"""
    config_manager = config_manager_plugin.get_config_manager(
        "komari_knowledge",
        KnowledgeConfigSchema,
    )
    config = config_manager.get()
    api_token = getattr(config, "api_token", "")
    if not isinstance(api_token, str) or not api_token.strip():
        logger.warning("[Komari Management] 未配置共享 api_token，跳过管理 API 注册")
        return None

    return SharedManagementSettings(
        api_token=api_token.strip(),
        allowed_origins=normalize_origins(getattr(config, "api_allowed_origins", [])),
    )
