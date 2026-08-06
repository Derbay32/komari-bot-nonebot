"""Komari Search 提供者配置描述 REST API。"""

from __future__ import annotations

from collections.abc import Mapping
from types import UnionType
from typing import TYPE_CHECKING, Any, Literal, Union, get_args, get_origin

from fastapi import APIRouter, Depends, FastAPI
from pydantic import BaseModel
from pydantic_core import PydanticUndefined

from komari_bot.common.management_api import (
    create_bearer_auth_dependency,
    ensure_management_cors,
)

from .config_schema import DynamicConfigSchema

if TYPE_CHECKING:
    from collections.abc import Sequence

    from komari_bot.common.management_api import ManagementTokenSource

API_PREFIX = "/api/v2/komari-search"
_PROVIDER_PREFIXES = {
    "tavily": "tavily_",
    "exa": "exa_",
}
_METADATA_FIELDS = {
    # 强类型表存储专用字段（单行主键 / CAS 修订号 / 写入时间）不进入
    # 管理描述符；白名单与插件开关同样不属于搜索字段描述面。
    "id",
    "revision",
    "updated_at",
    "plugin_enable",
    "user_whitelist",
    "group_whitelist",
}


class ProviderFieldDescriptor(BaseModel):
    """可供管理前端渲染的单个配置字段描述。"""

    field_name: str
    field_type: str
    description: str
    default: Any
    secret: bool = False


class ProviderDescriptor(BaseModel):
    """单个搜索提供者的专用字段集合。"""

    provider_id: str
    fields: list[ProviderFieldDescriptor]


class ProviderDescriptorsResponse(BaseModel):
    """搜索提供者及其动态配置字段描述。"""

    current_provider: str
    available_providers: list[str]
    common_fields: list[ProviderFieldDescriptor]
    providers: list[ProviderDescriptor]


def _format_field_type(annotation: object) -> str:
    """把字段注解转换为稳定、紧凑的前端描述。"""
    origin = get_origin(annotation)
    if origin is Literal:
        values = ", ".join(repr(value) for value in get_args(annotation))
        return f"Literal[{values}]"
    if origin in {Union, UnionType}:
        return " | ".join(_format_field_type(item) for item in get_args(annotation))
    if origin is not None:
        name = getattr(origin, "__name__", str(origin).replace("typing.", ""))
        arguments = get_args(annotation)
        if arguments:
            formatted = ", ".join(_format_field_type(item) for item in arguments)
            return f"{name}[{formatted}]"
        return name
    return getattr(annotation, "__name__", str(annotation).replace("typing.", ""))


def _field_default(field_name: str) -> Any:
    field = DynamicConfigSchema.model_fields[field_name]
    if field.default is not PydanticUndefined:
        return field.default
    return field.get_default(call_default_factory=True, validated_data={})


def _build_provider_descriptors() -> ProviderDescriptorsResponse:
    """从配置 Schema 反射生成提供者字段分组。"""
    common_fields: list[ProviderFieldDescriptor] = []
    provider_fields: dict[str, list[ProviderFieldDescriptor]] = {
        provider_id: [] for provider_id in _PROVIDER_PREFIXES
    }

    for field_name, field in DynamicConfigSchema.model_fields.items():
        if field_name in _METADATA_FIELDS:
            continue
        schema_extra = field.json_schema_extra
        secret = False
        if isinstance(schema_extra, Mapping):
            secret = bool(schema_extra.get("secret", False))
        descriptor = ProviderFieldDescriptor(
            field_name=field_name,
            field_type=_format_field_type(field.annotation),
            description=field.description or "",
            default=_field_default(field_name),
            secret=secret,
        )
        for provider_id, prefix in _PROVIDER_PREFIXES.items():
            if field_name.startswith(prefix):
                provider_fields[provider_id].append(descriptor)
                break
        else:
            common_fields.append(descriptor)

    available_providers = list(_PROVIDER_PREFIXES)
    return ProviderDescriptorsResponse(
        current_provider=DynamicConfigSchema().search_provider,
        available_providers=available_providers,
        common_fields=common_fields,
        providers=[
            ProviderDescriptor(
                provider_id=provider_id,
                fields=provider_fields[provider_id],
            )
            for provider_id in available_providers
        ],
    )


def create_search_router(*, api_token: ManagementTokenSource) -> APIRouter:
    """创建搜索提供者描述路由。"""
    auth_dependency = create_bearer_auth_dependency(
        api_token,
        detail="未授权访问 Komari Search 管理接口",
        required_permission="search:read",
    )
    router = APIRouter(
        prefix=API_PREFIX,
        dependencies=[Depends(auth_dependency)],
        tags=["komari-search"],
    )

    @router.get(
        "/provider-descriptors",
        response_model=ProviderDescriptorsResponse,
    )
    async def get_provider_descriptors() -> ProviderDescriptorsResponse:
        return _build_provider_descriptors()

    return router


def register_search_api(
    app: FastAPI,
    *,
    api_token: ManagementTokenSource,
    allowed_origins: Sequence[str],
) -> None:
    """注册搜索提供者描述 API 与共享 CORS。"""
    if getattr(app.state, "komari_search_api_registered", False):
        return

    ensure_management_cors(app, allowed_origins)
    app.include_router(create_search_router(api_token=api_token))
    app.state.komari_search_api_registered = True


__all__ = [
    "API_PREFIX",
    "ProviderDescriptor",
    "ProviderDescriptorsResponse",
    "ProviderFieldDescriptor",
    "create_search_router",
    "register_search_api",
]
