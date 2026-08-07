"""Komari Management Prompt REST API。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Body, Depends, FastAPI, Header, HTTPException
from fastapi import Path as ApiPath
from nonebot import logger
from pydantic import BaseModel, ConfigDict, Field
from starlette import status

from komari_bot.config.prompt_storage import (
    PromptValues,
    StoredPrompt,
    load_prompt_values_async,
    merge_prompt_values,
    replace_prompt_values_async,
    update_prompt_field_async,
)
from komari_bot.config.typed_config import ensure_typed_prompt_model
from komari_bot.management.management_api import (
    ManagementPrincipal,
    create_bearer_auth_dependency,
    ensure_management_cors,
)
from komari_bot.management.management_audit import (
    hash_management_target,
    management_audit_span,
    record_management_audit_event,
    require_management_change_reason,
    resolve_management_request_id,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from komari_bot.management.management_api import ManagementTokenSource
    from komari_bot.management.management_audit import ManagementAuditRecorder

    from .managed_resources import ManagedPromptResource

API_PREFIX = "/api/v2/komari-management-prompt"
_WEAK_ETAG_ERROR = "If-Match 必须使用强 ETag"
_INVALID_ETAG_ERROR = "If-Match 必须是非负整数 revision"


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
    revision: int


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


def _conflict(detail: str = "提示词配置已被其他请求修改，请重新读取后重试") -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _parse_expected_revision(if_match: str) -> int:
    """解析管理 API 的强 ETag revision。"""
    raw = if_match.strip()
    if raw.startswith("W/"):
        raise _validation_error(_WEAK_ETAG_ERROR)
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        raw = raw[1:-1]
    if not raw.isdigit():
        raise _validation_error(_INVALID_ETAG_ERROR)
    return int(raw)


def _get_resource_map(
    resources: Sequence[ManagedPromptResource],
) -> dict[str, ManagedPromptResource]:
    return {resource.resource_id: resource for resource in resources}


async def _load_prompt_values(resource: ManagedPromptResource) -> PromptValues:
    try:
        return await load_prompt_values_async(resource)
    except Exception as exc:
        logger.exception("读取提示词配置失败: {}", resource.resource_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="读取提示词配置失败",
        ) from exc


async def _replace_prompt_values(
    resource: ManagedPromptResource,
    values: dict[str, object],
    *,
    expected_revision: int,
) -> StoredPrompt:
    try:
        stored = await replace_prompt_values_async(
            resource,
            values,
            expected_revision=expected_revision,
        )
    except ValueError as exc:
        raise _validation_error(str(exc)) from exc
    except Exception as exc:
        logger.exception("保存提示词配置失败: {}", resource.resource_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="保存提示词配置失败",
        ) from exc
    if stored is None:
        raise _conflict()
    return stored


async def _update_prompt_field(
    resource: ManagedPromptResource,
    field_name: str,
    value: str,
    *,
    expected_revision: int,
) -> StoredPrompt:
    try:
        stored = await update_prompt_field_async(
            resource,
            field_name,
            value,
            expected_revision=expected_revision,
        )
    except ValueError as exc:
        raise _validation_error(str(exc)) from exc
    except Exception as exc:
        logger.exception("更新提示词字段失败: {}", resource.resource_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="更新提示词字段失败",
        ) from exc
    if stored is None:
        raise _conflict()
    return stored


def _build_config_source(resource: ManagedPromptResource) -> str:
    """Prompt 资源的存储来源标识，表名取自强类型 Prompt 表。"""
    model_cls = ensure_typed_prompt_model(resource.resource_id)
    if model_cls is None:
        msg = f"Prompt 资源 {resource.resource_id} 未注册强类型 Prompt 表"
        raise RuntimeError(msg)
    return f"postgresql:{model_cls.__tablename__}:{resource.resource_id}"


def _build_resource_summary(resource: ManagedPromptResource) -> PromptResourceSummary:
    return PromptResourceSummary(
        resource_id=resource.resource_id,
        display_name=resource.display_name,
        config_source=_build_config_source(resource),
        storage_key=resource.resource_id,
        file_path=None,
        fields=sorted(resource.defaults),
    )


def _build_resource_detail(
    resource: ManagedPromptResource,
    loaded: PromptValues | StoredPrompt,
) -> PromptResourceDetail:
    if isinstance(loaded, PromptValues):
        values = loaded.values
        revision = loaded.stored.revision if loaded.stored is not None else 0
    else:
        values = merge_prompt_values(resource.defaults, loaded.prompt_data)
        revision = loaded.revision
    return PromptResourceDetail(
        resource_id=resource.resource_id,
        display_name=resource.display_name,
        config_source=_build_config_source(resource),
        storage_key=resource.resource_id,
        file_path=None,
        fields=sorted(values.keys()),
        values=values,
        revision=revision,
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
    audit_recorder: ManagementAuditRecorder | None = None,
) -> APIRouter:
    """创建提示词管理路由。"""
    auth_dependency = create_bearer_auth_dependency(
        api_token,
        detail="未授权访问 Komari Management Prompt 接口",
        required_permission="prompt:read",
    )
    write_auth_dependency = create_bearer_auth_dependency(
        api_token,
        detail="未授权修改 Komari Management Prompt",
        required_permission="prompt:write",
    )
    recorder = audit_recorder or record_management_audit_event
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
        loaded = await _load_prompt_values(resource)
        return _build_resource_detail(resource, loaded)

    @router.put("/resources/{resource_id}", response_model=PromptResourceDetail)
    async def replace_prompt_resource(
        resource_id: Annotated[str, ApiPath(min_length=1)],
        payload: Annotated[dict[str, object], Body()],
        if_match: Annotated[str, Header(alias="If-Match")],
        principal: ManagementPrincipal = Depends(write_auth_dependency),  # noqa: FAST002
        reason: str = Depends(require_management_change_reason),  # noqa: FAST002
        request_id: str = Depends(resolve_management_request_id),  # noqa: FAST002
    ) -> PromptResourceDetail:
        async with management_audit_span(
            principal=principal,
            request_id=request_id,
            reason=reason,
            action="prompt.replace",
            resource=resource_id,
            target_hash=hash_management_target(resource_id),
            recorder=recorder,
        ):
            resource = _resolve_resource(resource_id, resource_map)
            stored = await _replace_prompt_values(
                resource,
                payload,
                expected_revision=_parse_expected_revision(if_match),
            )
            return _build_resource_detail(resource, stored)

    @router.patch(
        "/resources/{resource_id}/fields/{field_name}",
        response_model=PromptResourceDetail,
    )
    async def update_prompt_field(
        resource_id: Annotated[str, ApiPath(min_length=1)],
        field_name: Annotated[str, ApiPath(min_length=1)],
        payload: Annotated[PromptFieldUpdateRequest, Body()],
        if_match: Annotated[str, Header(alias="If-Match")],
        principal: ManagementPrincipal = Depends(write_auth_dependency),  # noqa: FAST002
        reason: str = Depends(require_management_change_reason),  # noqa: FAST002
        request_id: str = Depends(resolve_management_request_id),  # noqa: FAST002
    ) -> PromptResourceDetail:
        async with management_audit_span(
            principal=principal,
            request_id=request_id,
            reason=reason,
            action="prompt.update_field",
            resource=resource_id,
            field_name=field_name,
            target_hash=hash_management_target(resource_id, field_name),
            recorder=recorder,
        ):
            resource = _resolve_resource(resource_id, resource_map)
            if field_name not in resource.defaults:
                detail = f"未找到提示词字段: {field_name}"
                raise _not_found(detail)
            stored = await _update_prompt_field(
                resource,
                field_name,
                payload.value,
                expected_revision=_parse_expected_revision(if_match),
            )
            return _build_resource_detail(resource, stored)

    return router


def register_prompt_api(
    app: FastAPI,
    *,
    api_token: ManagementTokenSource,
    allowed_origins: Sequence[str],
    resources: Sequence[ManagedPromptResource],
    audit_recorder: ManagementAuditRecorder | None = None,
) -> None:
    """注册提示词管理 API。"""
    if getattr(app.state, "komari_management_prompt_api_registered", False):
        return

    ensure_management_cors(app, allowed_origins)
    app.include_router(
        create_prompt_router(
            api_token=api_token,
            resources=resources,
            audit_recorder=audit_recorder,
        )
    )
    app.state.komari_management_prompt_api_registered = True
