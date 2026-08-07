"""Komari Management 配置 REST API。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field
from starlette import status

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
from komari_bot.plugins.config_manager.manager import ConfigUpdateConflictError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from komari_bot.management.management_api import ManagementTokenSource
    from komari_bot.management.management_audit import ManagementAuditRecorder

    from .managed_resources import ManagedConfigResource

API_PREFIX = "/api/v2/komari-management-config"
_MASKED_CONFIG_VALUE = "******"

ConfigApplyMode = Literal["immediate", "rebuild", "restart"]
EffectiveValueSource = Literal[
    "dynamic_config",
    "service_snapshot",
    "process_startup_snapshot",
]
_DEFAULT_APPLY_MODE: ConfigApplyMode = "restart"


class ConfigFieldMetadata(BaseModel):
    """配置字段的安全与生效方式元数据。"""

    secret: bool
    apply_mode: ConfigApplyMode


class ConfigFieldState(ConfigFieldMetadata):
    """配置字段的持久化值与当前可确认生效状态。"""

    configured_value: Any
    effective_value: Any
    source: str
    effective_source: EffectiveValueSource
    restart_required: bool


class ConfigResourceSummary(BaseModel):
    """配置资源摘要。"""

    resource_id: str
    display_name: str
    config_source: str
    fields: list[str]
    field_descriptions: dict[str, str]
    field_metadata: dict[str, ConfigFieldMetadata]


class ConfigResourceDetail(ConfigResourceSummary):
    """配置资源详情。"""

    values: dict[str, Any]
    field_states: dict[str, ConfigFieldState]


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


def _conflict(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=detail,
    )


def _get_resource_map(
    resources: Sequence[ManagedConfigResource],
) -> dict[str, ManagedConfigResource]:
    return {resource.resource_id: resource for resource in resources}


def _get_fields(config: BaseModel) -> list[str]:
    return sorted(config.model_dump().keys())


def _get_field_descriptions(config: BaseModel) -> dict[str, str]:
    public_fields = set(config.model_dump())
    return {
        field_name: (field_info.description or "")
        for field_name, field_info in sorted(config.__class__.model_fields.items())
        if field_name in public_fields
    }


def _parse_apply_mode(value: object) -> ConfigApplyMode:
    """解析生效模式；无效或缺失声明按需重启处理。"""
    match value:
        case "immediate":
            return "immediate"
        case "rebuild":
            return "rebuild"
        case "restart":
            return "restart"
        case _:
            return _DEFAULT_APPLY_MODE


def _get_default_apply_mode(config: BaseModel) -> ConfigApplyMode:
    schema_extra = config.__class__.model_config.get("json_schema_extra")
    if not isinstance(schema_extra, dict):
        return _DEFAULT_APPLY_MODE
    return _parse_apply_mode(schema_extra.get("default_apply_mode"))


def _get_field_metadata(config: BaseModel) -> dict[str, ConfigFieldMetadata]:
    default_apply_mode = _get_default_apply_mode(config)
    public_fields = set(config.model_dump())
    metadata: dict[str, ConfigFieldMetadata] = {}
    for field_name, field_info in config.__class__.model_fields.items():
        if field_name not in public_fields:
            continue
        field_extra = field_info.json_schema_extra
        extra = field_extra if isinstance(field_extra, dict) else {}
        metadata[field_name] = ConfigFieldMetadata(
            secret=extra.get("secret") is True,
            apply_mode=_parse_apply_mode(
                extra.get("apply_mode", default_apply_mode),
            ),
        )
    return metadata


def _mask_config_values(
    values: dict[str, Any],
    field_metadata: dict[str, ConfigFieldMetadata],
) -> dict[str, Any]:
    return {
        field_name: (
            _MASKED_CONFIG_VALUE
            if field_metadata[field_name].secret
            else value
        )
        for field_name, value in values.items()
    }


def _build_field_states(
    *,
    values: dict[str, Any],
    field_metadata: dict[str, ConfigFieldMetadata],
    config_source: str,
) -> dict[str, ConfigFieldState]:
    masked_values = _mask_config_values(values, field_metadata)
    states: dict[str, ConfigFieldState] = {}
    for field_name, configured_value in masked_values.items():
        metadata = field_metadata[field_name]
        match metadata.apply_mode:
            case "immediate":
                effective_value = configured_value
                effective_source: EffectiveValueSource = "dynamic_config"
            case "rebuild":
                effective_value = None
                effective_source = "service_snapshot"
            case "restart":
                effective_value = None
                effective_source = "process_startup_snapshot"
        states[field_name] = ConfigFieldState(
            configured_value=configured_value,
            effective_value=effective_value,
            source=config_source,
            effective_source=effective_source,
            secret=metadata.secret,
            apply_mode=metadata.apply_mode,
            restart_required=metadata.apply_mode == "restart",
        )
    return states


async def _build_resource_summary(
    resource: ManagedConfigResource,
) -> ConfigResourceSummary:
    manager = resource.manager_getter()
    config = await manager.get_async()
    field_metadata = _get_field_metadata(config)
    return ConfigResourceSummary(
        resource_id=resource.resource_id,
        display_name=resource.display_name,
        config_source=manager.config_source,
        fields=_get_fields(config),
        field_descriptions=_get_field_descriptions(config),
        field_metadata=field_metadata,
    )


async def _build_resource_detail(
    resource: ManagedConfigResource,
) -> ConfigResourceDetail:
    manager = resource.manager_getter()
    config = await manager.get_async()
    values = config.model_dump()
    field_metadata = _get_field_metadata(config)
    return ConfigResourceDetail(
        resource_id=resource.resource_id,
        display_name=resource.display_name,
        config_source=manager.config_source,
        fields=_get_fields(config),
        field_descriptions=_get_field_descriptions(config),
        field_metadata=field_metadata,
        values=_mask_config_values(values, field_metadata),
        field_states=_build_field_states(
            values=values,
            field_metadata=field_metadata,
            config_source=manager.config_source,
        ),
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
    api_token: ManagementTokenSource,
    resources: Sequence[ManagedConfigResource],
    audit_recorder: ManagementAuditRecorder | None = None,
) -> APIRouter:
    """创建配置文件管理路由。"""
    auth_dependency = create_bearer_auth_dependency(
        api_token,
        detail="未授权访问 Komari Management 配置接口",
        required_permission="config:read",
    )
    write_auth_dependency = create_bearer_auth_dependency(
        api_token,
        detail="未授权修改 Komari Management 配置",
        required_permission="config:write",
    )
    recorder = audit_recorder or record_management_audit_event
    resource_map = _get_resource_map(resources)
    router = APIRouter(
        prefix=API_PREFIX,
        dependencies=[Depends(auth_dependency)],
        tags=["komari-management-config"],
    )

    @router.get("/resources", response_model=ConfigResourceListResponse)
    async def list_config_resources() -> ConfigResourceListResponse:
        items = [await _build_resource_summary(resource) for resource in resources]
        return ConfigResourceListResponse(items=items, total=len(items))

    @router.get("/resources/{resource_id}", response_model=ConfigResourceDetail)
    async def get_config_resource(
        resource_id: Annotated[str, Path(min_length=1)],
    ) -> ConfigResourceDetail:
        resource = _resolve_resource(resource_id, resource_map)
        return await _build_resource_detail(resource)

    @router.post("/resources/{resource_id}/reload", response_model=ConfigResourceDetail)
    async def reload_config_resource(
        resource_id: Annotated[str, Path(min_length=1)],
        principal: ManagementPrincipal = Depends(write_auth_dependency),  # noqa: FAST002
        reason: str = Depends(require_management_change_reason),  # noqa: FAST002
        request_id: str = Depends(resolve_management_request_id),  # noqa: FAST002
    ) -> ConfigResourceDetail:
        async with management_audit_span(
            principal=principal,
            request_id=request_id,
            reason=reason,
            action="config.reload",
            resource=resource_id,
            target_hash=hash_management_target(resource_id),
            recorder=recorder,
        ):
            resource = _resolve_resource(resource_id, resource_map)
            await resource.manager_getter().reload_async()
            return await _build_resource_detail(resource)

    @router.patch(
        "/resources/{resource_id}/fields/{field_name}",
        response_model=ConfigResourceDetail,
    )
    async def update_config_field(
        resource_id: Annotated[str, Path(min_length=1)],
        field_name: Annotated[str, Path(min_length=1)],
        payload: Annotated[ConfigFieldUpdateRequest, Body()],
        principal: ManagementPrincipal = Depends(write_auth_dependency),  # noqa: FAST002
        reason: str = Depends(require_management_change_reason),  # noqa: FAST002
        request_id: str = Depends(resolve_management_request_id),  # noqa: FAST002
    ) -> ConfigResourceDetail:
        async with management_audit_span(
            principal=principal,
            request_id=request_id,
            reason=reason,
            action="config.update_field",
            resource=resource_id,
            field_name=field_name,
            target_hash=hash_management_target(resource_id, field_name),
            recorder=recorder,
        ):
            resource = _resolve_resource(resource_id, resource_map)
            manager = resource.manager_getter()
            try:
                await manager.update_field_async(field_name, payload.value)
            except ValueError as exc:
                raise _validation_error(str(exc)) from exc
            except ConfigUpdateConflictError as exc:
                raise _conflict(str(exc)) from exc
            return await _build_resource_detail(resource)

    return router


def register_config_api(
    app: FastAPI,
    *,
    api_token: ManagementTokenSource,
    allowed_origins: Sequence[str],
    resources: Sequence[ManagedConfigResource],
    audit_recorder: ManagementAuditRecorder | None = None,
) -> None:
    """注册配置管理 API。"""
    if getattr(app.state, "komari_management_config_api_registered", False):
        return

    ensure_management_cors(app, allowed_origins)
    app.include_router(
        create_config_router(
            api_token=api_token,
            resources=resources,
            audit_recorder=audit_recorder,
        )
    )
    app.state.komari_management_config_api_registered = True
