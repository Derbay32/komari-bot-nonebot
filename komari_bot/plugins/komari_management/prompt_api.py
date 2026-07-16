"""Komari Management Prompt REST API。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException
from fastapi import Path as ApiPath
from nonebot import logger
from pydantic import BaseModel, ConfigDict, Field
from starlette import status

from komari_bot.common.management_api import (
    create_bearer_auth_dependency,
    ensure_management_cors,
)
from komari_bot.common.prompt_storage import load_prompt_values, save_prompt_values

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from komari_bot.common.management_api import ManagementTokenSource

    from .managed_resources import ManagedPromptResource

API_PREFIX = "/api/komari-management-prompt/v1"


class PromptResourceSummary(BaseModel):
    """提示词资源摘要。"""

    resource_id: str
    display_name: str
    config_source: str
    storage_key: str
    file_path: str | None = None
    fields: list[str]


class PromptResourceDetail(PromptResourceSummary):
    """提示词资源详情。"""

    values: dict[str, str]


class PromptResourceListResponse(BaseModel):
    """提示词资源列表响应。"""

    items: list[PromptResourceSummary]
    total: int


class PromptFieldUpdateRequest(BaseModel):
    """提示词字段更新请求。"""

    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1, description="新的提示词内容")


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
    resources: Sequence[ManagedPromptResource],
) -> dict[str, ManagedPromptResource]:
    return {resource.resource_id: resource for resource in resources}


def _load_prompt_values(resource: ManagedPromptResource) -> dict[str, str]:
    try:
        return load_prompt_values(resource).values
    except Exception as exc:
        logger.exception("读取提示词配置失败: {}", resource.resource_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="读取提示词配置失败",
        ) from exc


def _save_prompt_values(
    resource: ManagedPromptResource, values: Mapping[str, object]
) -> None:
    try:
        save_prompt_values(resource, values)
    except ValueError as exc:
        raise _validation_error(str(exc)) from exc
    except Exception as exc:
        logger.exception("保存提示词配置失败: {}", resource.resource_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="保存提示词配置失败",
        ) from exc


def _build_config_source(resource: ManagedPromptResource) -> str:
    return f"postgresql:komari_prompt_configs:{resource.resource_id}"


def _build_resource_summary(resource: ManagedPromptResource) -> PromptResourceSummary:
    values = _load_prompt_values(resource)
    return PromptResourceSummary(
        resource_id=resource.resource_id,
        display_name=resource.display_name,
        config_source=_build_config_source(resource),
        storage_key=resource.resource_id,
        file_path=None,
        fields=sorted(values.keys()),
    )


def _build_resource_detail(resource: ManagedPromptResource) -> PromptResourceDetail:
    values = _load_prompt_values(resource)
    return PromptResourceDetail(
        resource_id=resource.resource_id,
        display_name=resource.display_name,
        config_source=_build_config_source(resource),
        storage_key=resource.resource_id,
        file_path=None,
        fields=sorted(values.keys()),
        values=values,
    )


def _resolve_resource(
    resource_id: str,
    resource_map: dict[str, ManagedPromptResource],
) -> ManagedPromptResource:
    resource = resource_map.get(resource_id)
    if resource is None:
        detail = f"未找到提示词资源: {resource_id}"
        raise _not_found(detail)
    return resource


def create_prompt_router(
    *,
    api_token: ManagementTokenSource,
    resources: Sequence[ManagedPromptResource],
) -> APIRouter:
    """创建提示词管理路由。"""
    auth_dependency = create_bearer_auth_dependency(
        api_token,
        detail="未授权访问 Komari Management Prompt 接口",
    )
    resource_map = _get_resource_map(resources)
    router = APIRouter(
        prefix=API_PREFIX,
        dependencies=[Depends(auth_dependency)],
        tags=["komari-management-prompt"],
    )

    @router.get("/resources", response_model=PromptResourceListResponse)
    async def list_prompt_resources() -> PromptResourceListResponse:
        items = [_build_resource_summary(resource) for resource in resources]
        return PromptResourceListResponse(items=items, total=len(items))

    @router.get("/resources/{resource_id}", response_model=PromptResourceDetail)
    async def get_prompt_resource(
        resource_id: Annotated[str, ApiPath(min_length=1)],
    ) -> PromptResourceDetail:
        resource = _resolve_resource(resource_id, resource_map)
        return _build_resource_detail(resource)

    @router.put("/resources/{resource_id}", response_model=PromptResourceDetail)
    async def replace_prompt_resource(
        resource_id: Annotated[str, ApiPath(min_length=1)],
        payload: Annotated[dict[str, object], Body()],
    ) -> PromptResourceDetail:
        resource = _resolve_resource(resource_id, resource_map)
        _save_prompt_values(resource, payload)
        return _build_resource_detail(resource)

    @router.patch(
        "/resources/{resource_id}/fields/{field_name}",
        response_model=PromptResourceDetail,
    )
    async def update_prompt_field(
        resource_id: Annotated[str, ApiPath(min_length=1)],
        field_name: Annotated[str, ApiPath(min_length=1)],
        payload: Annotated[PromptFieldUpdateRequest, Body()],
    ) -> PromptResourceDetail:
        resource = _resolve_resource(resource_id, resource_map)
        if field_name not in resource.defaults:
            detail = f"未找到提示词字段: {field_name}"
            raise _not_found(detail)
        values = _load_prompt_values(resource)
        values[field_name] = payload.value.rstrip("\n")
        _save_prompt_values(resource, dict(values))
        return _build_resource_detail(resource)

    return router


def register_prompt_api(
    app: FastAPI,
    *,
    api_token: ManagementTokenSource,
    allowed_origins: Sequence[str],
    resources: Sequence[ManagedPromptResource],
) -> None:
    """注册提示词管理 API。"""
    if getattr(app.state, "komari_management_prompt_api_registered", False):
        return

    ensure_management_cors(app, allowed_origins)
    app.include_router(create_prompt_router(api_token=api_token, resources=resources))
    app.state.komari_management_prompt_api_registered = True
