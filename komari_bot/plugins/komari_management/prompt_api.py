"""Komari Management Prompt REST API。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import yaml
from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException
from fastapi import Path as ApiPath
from pydantic import BaseModel, ConfigDict, Field
from starlette import status

from komari_bot.common.management_api import (
    create_bearer_auth_dependency,
    ensure_management_cors,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from .managed_resources import ManagedPromptResource

API_PREFIX = "/api/komari-management-prompt/v1"


class PromptResourceSummary(BaseModel):
    """提示词资源摘要。"""

    resource_id: str
    display_name: str
    file_path: str
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


def _resolve_path(path: Path) -> Path:
    if path.is_absolute():
        return path
    return path.resolve()


def _load_prompt_values(resource: ManagedPromptResource) -> dict[str, str]:
    path = _resolve_path(resource.file_path)
    values = dict(resource.defaults)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return values
    except yaml.YAMLError as exc:
        detail = f"提示词文件 YAML 解析失败: {exc}"
        raise _validation_error(detail) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"读取提示词文件失败: {exc}",
        ) from exc

    if not isinstance(data, dict):
        raise _validation_error("提示词文件内容必须是对象")
    for key in resource.defaults:
        value = data.get(key)
        if isinstance(value, str):
            values[key] = value.rstrip("\n")
    return values


def _save_prompt_values(
    resource: ManagedPromptResource, values: dict[str, str]
) -> None:
    path = _resolve_path(resource.file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            values,
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _build_resource_summary(resource: ManagedPromptResource) -> PromptResourceSummary:
    values = _load_prompt_values(resource)
    return PromptResourceSummary(
        resource_id=resource.resource_id,
        display_name=resource.display_name,
        file_path=str(_resolve_path(resource.file_path)),
        fields=sorted(values.keys()),
    )


def _build_resource_detail(resource: ManagedPromptResource) -> PromptResourceDetail:
    values = _load_prompt_values(resource)
    return PromptResourceDetail(
        resource_id=resource.resource_id,
        display_name=resource.display_name,
        file_path=str(_resolve_path(resource.file_path)),
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
    api_token: str,
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
        payload: Annotated[dict[str, str], Body()],
    ) -> PromptResourceDetail:
        resource = _resolve_resource(resource_id, resource_map)
        unknown_fields = sorted(set(payload) - set(resource.defaults))
        if unknown_fields:
            fields = ", ".join(unknown_fields)
            detail = f"存在未知提示词字段: {fields}"
            raise _validation_error(detail)
        values = dict(resource.defaults)
        for key, value in payload.items():
            if not isinstance(value, str) or not value.strip():
                detail = f"提示词字段 {key} 必须是非空字符串"
                raise _validation_error(detail)
            values[key] = value.rstrip("\n")
        _save_prompt_values(resource, values)
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
        _save_prompt_values(resource, values)
        return _build_resource_detail(resource)

    return router


def register_prompt_api(
    app: FastAPI,
    *,
    api_token: str,
    allowed_origins: Sequence[str],
    resources: Sequence[ManagedPromptResource],
) -> None:
    """注册提示词管理 API。"""
    if getattr(app.state, "komari_management_prompt_api_registered", False):
        return

    ensure_management_cors(app, allowed_origins)
    app.include_router(create_prompt_router(api_token=api_token, resources=resources))
    app.state.komari_management_prompt_api_registered = True
