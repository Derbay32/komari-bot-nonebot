"""Komari Decision scenes 管理 REST API。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal, cast

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException
from fastapi import Path as ApiPath
from nonebot import logger
from nonebot.plugin import require
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
from komari_bot.plugins.komari_decision import get_scene_admin_service

if TYPE_CHECKING:
    from collections.abc import Sequence

    from komari_bot.management.management_api import ManagementTokenSource
    from komari_bot.management.management_audit import ManagementAuditRecorder

API_PREFIX = "/api/v2/komari-decision-scenes"
_REQUIRED_FIXED_KEYS = {"NOISE", "MEANINGFUL", "CALL_DIRECT", "CALL_MENTION"}


class SceneSummary(BaseModel):
    """Scene 摘要。"""

    scene_key: str
    scene_type: Literal["fixed", "general"]
    enabled: bool
    order_index: int
    content_hash: str
    updated_at: str


class SceneDetail(SceneSummary):
    """Scene 详情。"""

    content_text: str


class SceneListResponse(BaseModel):
    """Scene 列表响应。"""

    items: list[SceneSummary]
    total: int


class ScenePutRequest(BaseModel):
    """Scene 全量更新请求。"""

    model_config = ConfigDict(extra="forbid")

    scene_type: Literal["fixed", "general"]
    content_text: str = Field(min_length=1)
    enabled: bool = True
    order_index: int = 0


class ScenePatchRequest(BaseModel):
    """Scene 局部更新请求。"""

    model_config = ConfigDict(extra="forbid")

    content_text: str | None = Field(default=None, min_length=1)
    enabled: bool | None = None
    order_index: int | None = None


class SceneSyncResponse(BaseModel):
    """Scene 同步触发响应。"""

    triggered: bool
    set_id: int | None = None
    created: bool | None = None
    reused_existing_set: bool | None = None
    inserted_count: int | None = None
    ready_count: int | None = None
    pending_count: int | None = None
    detail: str


def _validation_error(message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=message)


def _not_found(message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message)


def _serialize_summary(row: dict[str, Any]) -> SceneSummary:
    return SceneSummary(
        scene_key=str(row["scene_key"]),
        scene_type=cast("Literal['fixed', 'general']", str(row["scene_type"])),
        enabled=bool(row["enabled"]),
        order_index=int(row["order_index"]),
        content_hash=str(row["content_hash"]),
        updated_at=str(row["updated_at"]),
    )


def _serialize_detail(row: dict[str, Any]) -> SceneDetail:
    summary = _serialize_summary(row)
    return SceneDetail(
        **summary.model_dump(),
        content_text=str(row["content_text"]),
    )


def _prepare_admin_service() -> Any:
    """获取 scene 运维服务；判定插件未就绪时统一报服务未就绪。"""
    service = get_scene_admin_service()
    if service is None:
        logger.error("[Komari Management] scene 服务未就绪；请确认 komari_decision 已就绪")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="scene 服务未就绪；请确认 komari_decision 已就绪",
        )
    return service


def _validate_required_fixed_update(scene_key: str, scene_type: str, *, enabled: bool) -> None:
    if scene_key not in _REQUIRED_FIXED_KEYS:
        return
    if scene_type != "fixed":
        msg = f"必需 fixed scene 不允许改为其他类型: {scene_key}"
        raise _validation_error(msg)
    if not enabled:
        msg = f"必需 fixed scene 不允许禁用: {scene_key}"
        raise _validation_error(msg)


def create_scene_router(
    *,
    api_token: ManagementTokenSource,
    audit_recorder: ManagementAuditRecorder | None = None,
) -> APIRouter:
    """创建 Komari Decision scenes 管理路由。"""
    auth_dependency = create_bearer_auth_dependency(
        api_token,
        detail="未授权访问 Komari Decision Scenes 接口",
        required_permission="scene:read",
    )
    write_auth_dependency = create_bearer_auth_dependency(
        api_token,
        detail="未授权修改 Komari Decision Scenes",
        required_permission="scene:write",
    )
    recorder = audit_recorder or record_management_audit_event
    router = APIRouter(
        prefix=API_PREFIX,
        dependencies=[Depends(auth_dependency)],
        tags=["komari-decision-scenes"],
    )

    @router.get("/scenes", response_model=SceneListResponse)
    async def list_scenes() -> SceneListResponse:
        service = _prepare_admin_service()
        rows = await service.list_scenes(enabled_only=False)
        items = [_serialize_summary(row) for row in rows]
        return SceneListResponse(items=items, total=len(items))

    @router.get("/scenes/{scene_key}", response_model=SceneDetail)
    async def get_scene(scene_key: Annotated[str, ApiPath(min_length=1)]) -> SceneDetail:
        service = _prepare_admin_service()
        row = await service.get_scene_by_key(scene_key)
        if row is None:
            detail = f"scene 不存在: {scene_key}"
            raise _not_found(detail)
        return _serialize_detail(row)

    @router.put("/scenes/{scene_key}", response_model=SceneDetail)
    async def put_scene(
        scene_key: Annotated[str, ApiPath(min_length=1)],
        payload: Annotated[ScenePutRequest, Body()],
        principal: ManagementPrincipal = Depends(write_auth_dependency),  # noqa: FAST002
        reason: str = Depends(require_management_change_reason),  # noqa: FAST002
        request_id: str = Depends(resolve_management_request_id),  # noqa: FAST002
    ) -> SceneDetail:
        async with management_audit_span(
            principal=principal,
            request_id=request_id,
            reason=reason,
            action="scene.replace",
            resource="komari_decision_scene",
            field_name=scene_key,
            target_hash=hash_management_target(scene_key),
            recorder=recorder,
        ):
            _validate_required_fixed_update(
                scene_key,
                payload.scene_type,
                enabled=payload.enabled,
            )
            service = _prepare_admin_service()
            try:
                row = await service.upsert_scene(
                    scene_key=scene_key,
                    scene_type=payload.scene_type,
                    content_text=payload.content_text,
                    enabled=payload.enabled,
                    order_index=payload.order_index,
                )
            except ValueError as exc:
                raise _validation_error(str(exc)) from exc
            return _serialize_detail(row)

    @router.patch("/scenes/{scene_key}", response_model=SceneDetail)
    async def patch_scene(
        scene_key: Annotated[str, ApiPath(min_length=1)],
        payload: Annotated[ScenePatchRequest, Body()],
        principal: ManagementPrincipal = Depends(write_auth_dependency),  # noqa: FAST002
        reason: str = Depends(require_management_change_reason),  # noqa: FAST002
        request_id: str = Depends(resolve_management_request_id),  # noqa: FAST002
    ) -> SceneDetail:
        async with management_audit_span(
            principal=principal,
            request_id=request_id,
            reason=reason,
            action="scene.update",
            resource="komari_decision_scene",
            field_name=scene_key,
            target_hash=hash_management_target(scene_key),
            recorder=recorder,
        ):
            service = _prepare_admin_service()
            current = await service.get_scene_by_key(scene_key)
            if current is None:
                detail = f"scene 不存在: {scene_key}"
                raise _not_found(detail)
            scene_type = str(current["scene_type"])
            content_text = (
                payload.content_text
                if payload.content_text is not None
                else str(current["content_text"])
            )
            enabled = (
                payload.enabled
                if payload.enabled is not None
                else bool(current["enabled"])
            )
            order_index = (
                payload.order_index
                if payload.order_index is not None
                else int(current["order_index"])
            )
            _validate_required_fixed_update(scene_key, scene_type, enabled=enabled)
            try:
                row = await service.upsert_scene(
                    scene_key=scene_key,
                    scene_type=scene_type,
                    content_text=content_text,
                    enabled=enabled,
                    order_index=order_index,
                )
            except ValueError as exc:
                raise _validation_error(str(exc)) from exc
            return _serialize_detail(row)

    @router.post("/sync", response_model=SceneSyncResponse)
    async def sync_scenes(
        principal: ManagementPrincipal = Depends(write_auth_dependency),  # noqa: FAST002
        reason: str = Depends(require_management_change_reason),  # noqa: FAST002
        request_id: str = Depends(resolve_management_request_id),  # noqa: FAST002
    ) -> SceneSyncResponse:
        async with management_audit_span(
            principal=principal,
            request_id=request_id,
            reason=reason,
            action="scene.sync",
            resource="komari_decision_scene_set",
            recorder=recorder,
        ) as audit:
            decision_plugin = require("komari_decision")
            manager_getter = getattr(decision_plugin, "get_plugin_manager", None)
            manager = manager_getter() if callable(manager_getter) else None
            scene_sync = (
                getattr(manager, "scene_sync", None) if manager is not None else None
            )
            if scene_sync is None:
                audit.metadata["triggered"] = False
                response = SceneSyncResponse(
                    triggered=False,
                    detail="scene sync 服务未就绪；请确认 komari_decision scene 持久化已启用",
                )
            else:
                result = await scene_sync.build_scene_set()
                audit.metadata.update(
                    {
                        "triggered": True,
                        "created": bool(result.created),
                        "inserted_count": int(result.inserted_count),
                    }
                )
                response = SceneSyncResponse(
                    triggered=True,
                    set_id=result.set_id,
                    created=result.created,
                    reused_existing_set=result.reused_existing_set,
                    inserted_count=result.inserted_count,
                    ready_count=result.ready_count,
                    pending_count=result.pending_count,
                    detail="scene sync 已触发，embedding 可能由后台任务异步完成",
                )
            return response

    return router


def register_scene_api(
    app: FastAPI,
    *,
    api_token: ManagementTokenSource,
    allowed_origins: Sequence[str],
    audit_recorder: ManagementAuditRecorder | None = None,
) -> None:
    """注册 Komari Decision scenes 管理 API。"""
    if getattr(app.state, "komari_decision_scene_api_registered", False):
        return
    ensure_management_cors(app, allowed_origins)
    app.include_router(
        create_scene_router(
            api_token=api_token,
            audit_recorder=audit_recorder,
        )
    )
    app.state.komari_decision_scene_api_registered = True
