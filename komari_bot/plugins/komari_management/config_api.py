"""Komari Management 配置文件 REST API。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field
from starlette import status

from komari_bot.common.management_api import (
    create_bearer_auth_dependency,
    ensure_management_cors,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .managed_resources import ManagedConfigResource

API_PREFIX = "/api/komari-management-config/v1"


class ConfigResourceSummary(BaseModel):
    """配置资源摘要。"""

    resource_id: str
    display_name: str
    config_file: str
    fields: list[str]


class ConfigResourceDetail(ConfigResourceSummary):
    """配置资源详情。"""

    values: dict[str, Any]


class ConfigResourceListResponse(BaseModel):
    """配置资源列表响应。"""

    items: list[ConfigResourceSummary]
    total: int


class ConfigFieldUpdateRequest(BaseModel):
    """配置字段更新请求。"""

    model_config = ConfigDict(extra="forbid")

    value: Any = Field(description="新的字段值")


def _validation_error(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=message,
    )


def _not_found(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=detail,
    )


def _get_resource_map(
    resources: Sequence[ManagedConfigResource],
) -> dict[str, ManagedConfigResource]:
    return {resource.resource_id: resource for resource in resources}


def _get_fields(config: BaseModel) -> list[str]:
    return sorted(config.model_dump().keys())


def _build_resource_summary(resource: ManagedConfigResource) -> ConfigResourceSummary:
    manager = resource.manager_getter()
    config = manager.get()
    return ConfigResourceSummary(
        resource_id=resource.resource_id,
        display_name=resource.display_name,
        config_file=str(manager.config_file),
        fields=_get_fields(config),
    )


def _build_resource_detail(resource: ManagedConfigResource) -> ConfigResourceDetail:
    manager = resource.manager_getter()
    config = manager.get()
    return ConfigResourceDetail(
        resource_id=resource.resource_id,
        display_name=resource.display_name,
        config_file=str(manager.config_file),
        fields=_get_fields(config),
        values=config.model_dump(),
    )


def _resolve_resource(
    resource_id: str,
    resource_map: dict[str, ManagedConfigResource],
) -> ManagedConfigResource:
    resource = resource_map.get(resource_id)
    if resource is None:
        detail = f"未找到配置资源: {resource_id}"
        raise _not_found(detail)
    return resource


def create_config_router(
    *,
    api_token: str,
    resources: Sequence[ManagedConfigResource],
) -> APIRouter:
    """创建配置文件管理路由。"""
    auth_dependency = create_bearer_auth_dependency(
        api_token,
        detail="未授权访问 Komari Management 配置接口",
    )
    resource_map = _get_resource_map(resources)
    router = APIRouter(
        prefix=API_PREFIX,
        dependencies=[Depends(auth_dependency)],
        tags=["komari-management-config"],
    )

    @router.get("/resources", response_model=ConfigResourceListResponse)
    async def list_config_resources() -> ConfigResourceListResponse:
        items = [_build_resource_summary(resource) for resource in resources]
        return ConfigResourceListResponse(items=items, total=len(items))

    @router.get("/resources/{resource_id}", response_model=ConfigResourceDetail)
    async def get_config_resource(
        resource_id: Annotated[str, Path(min_length=1)],
    ) -> ConfigResourceDetail:
        resource = _resolve_resource(resource_id, resource_map)
        return _build_resource_detail(resource)

    @router.post("/resources/{resource_id}/reload", response_model=ConfigResourceDetail)
    async def reload_config_resource(
        resource_id: Annotated[str, Path(min_length=1)],
    ) -> ConfigResourceDetail:
        resource = _resolve_resource(resource_id, resource_map)
        resource.manager_getter().reload_from_json()
        return _build_resource_detail(resource)

    @router.patch(
        "/resources/{resource_id}/fields/{field_name}",
        response_model=ConfigResourceDetail,
    )
    async def update_config_field(
        resource_id: Annotated[str, Path(min_length=1)],
        field_name: Annotated[str, Path(min_length=1)],
        payload: Annotated[ConfigFieldUpdateRequest, Body()],
    ) -> ConfigResourceDetail:
        resource = _resolve_resource(resource_id, resource_map)
        manager = resource.manager_getter()
        try:
            manager.update_field(field_name, payload.value)
        except ValueError as exc:
            raise _validation_error(str(exc)) from exc
        return _build_resource_detail(resource)

    return router


def register_config_api(
    app: FastAPI,
    *,
    api_token: str,
    allowed_origins: Sequence[str],
    resources: Sequence[ManagedConfigResource],
) -> None:
    """注册配置文件管理 API。"""
    if getattr(app.state, "komari_management_config_api_registered", False):
        return

    ensure_management_cors(app, allowed_origins)
    app.include_router(create_config_router(api_token=api_token, resources=resources))
    app.state.komari_management_config_api_registered = True
