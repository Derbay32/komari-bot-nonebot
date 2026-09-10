"""角色绑定受权修复 REST API（TSK-280）。

统一 /api/v2 控制面：``character_binding:read`` 仅诊断，``character_binding:
manage`` 才可预览/确认清除；变更必须有理由与请求 ID；审计只记录安全字段。
路由注册不依赖群准入或轮盘运行时：受限群、轮盘关闭时受权控制面仍可达。
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003
from typing import TYPE_CHECKING, Annotated, cast

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict

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
    require_management_request_id,
)

from .manager import BindingPersistenceError
from .repair import (
    BindingRepairService,
    RepairBlockedByGameError,
    RepairDependencyChangedError,
    RepairTargetNotFoundError,
    RepairTokenError,
    get_binding_repair_service,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from komari_bot.management.management_api import ManagementTokenSource
    from komari_bot.management.management_audit import (
        AuditMetadataValue,
        ManagementAuditRecorder,
    )

API_PREFIX = "/api/v2/character-bindings/repair"
_STORAGE_UNAVAILABLE = "绑定修复存储暂不可用"


class MemberBindingViewResponse(BaseModel):
    """诊断中的单个成员关系视图。"""

    model_config = ConfigDict(from_attributes=True)

    app_id: str
    group_openid: str
    member_openid: str
    member_qq: str
    character_name: str | None


class BindingDiagnosisResponse(BaseModel):
    """只读诊断响应。"""

    model_config = ConfigDict(from_attributes=True)

    app_id: str
    group_openid: str
    group_id: str
    members: list[MemberBindingViewResponse]
    game_present: bool
    game_lifecycle: str | None


class RepairPreviewResponse(BaseModel):
    """影响预览与确认令牌响应。"""

    model_config = ConfigDict(from_attributes=True)

    token: str
    scope: str
    app_id: str
    group_openid: str
    member_openid: str | None
    affected_count: int
    cleared_names: list[str | None]
    version: str
    expires_at: datetime


class RepairConfirmResultResponse(BaseModel):
    """确认清除结果响应。"""

    model_config = ConfigDict(from_attributes=True)

    scope: str
    app_id: str
    group_openid: str
    member_openid: str | None
    cleared_count: int
    cleared_names: list[str | None]
    version: str
    expected_count: int


class RepairPreviewRequest(BaseModel):
    """预览请求：未指定 member_openid 时为群级范围。"""

    model_config = ConfigDict(extra="forbid")

    app_id: str
    group_openid: str
    member_openid: str | None = None


class RepairConfirmRequest(BaseModel):
    """确认请求：令牌绑定目标与操作者。"""

    model_config = ConfigDict(extra="forbid")

    app_id: str
    group_openid: str
    token: str


def _require_repair_service(
    getter: Callable[[], object],
) -> BindingRepairService:
    service = cast("BindingRepairService | None", getter())
    if service is None:
        raise HTTPException(status_code=503, detail=_STORAGE_UNAVAILABLE)
    return service


def register_character_binding_repair_api(
    app: FastAPI,
    *,
    api_token: ManagementTokenSource,
    allowed_origins: Sequence[str],
    service_getter: Callable[[], object] | None = None,
    audit_recorder: ManagementAuditRecorder | None = None,
) -> None:
    """在统一管理应用上注册角色绑定修复 API（幂等）。"""
    if getattr(app.state, "komari_character_binding_repair_api_registered", False):
        return

    ensure_management_cors(app, allowed_origins)
    read_auth_dependency = create_bearer_auth_dependency(
        api_token,
        detail="未授权访问角色绑定修复接口",
        required_permission="character_binding:read",
    )
    manage_auth_dependency = create_bearer_auth_dependency(
        api_token,
        detail="没有修改角色绑定关系的权限",
        required_permission="character_binding:manage",
    )
    recorder = audit_recorder or record_management_audit_event
    getter: Callable[[], object] = service_getter or get_binding_repair_service

    router = APIRouter(
        prefix=API_PREFIX,
        tags=["character-binding-repair"],
    )

    @router.get(
        "/diagnose",
        response_model=BindingDiagnosisResponse,
        dependencies=[Depends(read_auth_dependency)],
    )
    async def diagnose(
        app_id: Annotated[str, Query()],
        group_openid: Annotated[str, Query()],
    ) -> BindingDiagnosisResponse:
        """只读诊断：群映射、成员视图与当前对局状态。"""
        service = _require_repair_service(getter)
        try:
            result = await service.diagnose(
                app_id=app_id,
                group_openid=group_openid,
            )
        except RepairTargetNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except BindingPersistenceError:
            raise HTTPException(status_code=503, detail=_STORAGE_UNAVAILABLE) from None
        return BindingDiagnosisResponse.model_validate(result)

    @router.post(
        "/preview",
        response_model=RepairPreviewResponse,
        dependencies=[
            Depends(read_auth_dependency),
            Depends(manage_auth_dependency),
        ],
    )
    async def preview(
        payload: Annotated[RepairPreviewRequest, Body()],
        principal: ManagementPrincipal = Depends(manage_auth_dependency),  # noqa: FAST002
        change_reason: str = Depends(require_management_change_reason),  # noqa: FAST002
        request_id: str = Depends(require_management_request_id),  # noqa: FAST002
    ) -> RepairPreviewResponse:
        """影响预览：明确范围/数量/将被清除的角色名，并签发一次性令牌。"""
        service = _require_repair_service(getter)
        target_hash = hash_management_target(
            payload.app_id,
            payload.group_openid,
            payload.member_openid or "<group>",
        )
        try:
            async with management_audit_span(
                principal=principal,
                request_id=request_id,
                reason=change_reason,
                action="character_binding.repair.preview",
                resource="character_binding",
                target_hash=target_hash,
                recorder=recorder,
            ) as audit:
                try:
                    result = await service.preview(
                        app_id=payload.app_id,
                        group_openid=payload.group_openid,
                        operator_id=principal.operator_id,
                        member_openid=payload.member_openid,
                        reason=change_reason,
                    )
                except RepairTargetNotFoundError as error:
                    raise HTTPException(status_code=404, detail=str(error)) from error
                except RepairTokenError as error:
                    raise HTTPException(status_code=422, detail=str(error)) from error
                except RepairDependencyChangedError as error:
                    raise HTTPException(status_code=409, detail=str(error)) from error
                except RepairBlockedByGameError as error:
                    raise HTTPException(status_code=409, detail=str(error)) from error
                except BindingPersistenceError:
                    raise HTTPException(
                        status_code=503, detail=_STORAGE_UNAVAILABLE
                    ) from None
                response = RepairPreviewResponse.model_validate(result)
                if response.scope == "group":
                    response.member_openid = None
                audit.metadata.update(
                    {
                        "scope": response.scope,
                        "version": response.version,
                        "affected_count": response.affected_count,
                        "result_code": "preview_issued",
                    }
                )
                return response
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(
                status_code=500,
                detail="审计记录暂不可用",
            ) from error

    @router.post(
        "/confirm",
        response_model=RepairConfirmResultResponse,
        dependencies=[
            Depends(read_auth_dependency),
            Depends(manage_auth_dependency),
        ],
    )
    async def confirm(
        payload: Annotated[RepairConfirmRequest, Body()],
        principal: ManagementPrincipal = Depends(manage_auth_dependency),  # noqa: FAST002
        change_reason: str = Depends(require_management_change_reason),  # noqa: FAST002
        request_id: str = Depends(require_management_request_id),  # noqa: FAST002
    ) -> RepairConfirmResultResponse:
        """确认清除：令牌绑定操作者/对象/版本，复核通过才原子删除。"""
        service = _require_repair_service(getter)
        try:
            # 开始审计 span 前先做同步只读、非消耗的令牌探针：合法令牌的真实
            # 成员目标与预览版本/预期数量在 ``started`` 即可见，失败路径也不
            # 依赖事后成功覆盖；无效令牌退化为请求群安全哈希。
            context = service.get_confirm_audit_context(
                app_id=payload.app_id,
                group_openid=payload.group_openid,
                token=payload.token,
                operator_id=principal.operator_id,
            )
            initial_metadata: dict[str, AuditMetadataValue]
            if context is None:
                target_hash = hash_management_target(
                    payload.app_id,
                    payload.group_openid,
                    "<group>",
                )
                initial_metadata = {}
            else:
                target_hash = hash_management_target(
                    payload.app_id,
                    payload.group_openid,
                    context.member_openid or "<group>",
                )
                initial_metadata = {
                    "scope": context.scope,
                    "version": context.version,
                    "expected_count": context.expected_count,
                }
            async with management_audit_span(
                principal=principal,
                request_id=request_id,
                reason=change_reason,
                action="character_binding.repair.confirm",
                resource="character_binding",
                target_hash=target_hash,
                initial_metadata=initial_metadata,
                recorder=recorder,
            ) as audit:
                try:
                    result = await service.confirm(
                        app_id=payload.app_id,
                        group_openid=payload.group_openid,
                        token=payload.token,
                        operator_id=principal.operator_id,
                        request_id=request_id,
                        reason=change_reason,
                    )
                except RepairTargetNotFoundError as error:
                    raise HTTPException(status_code=404, detail=str(error)) from error
                except RepairTokenError as error:
                    raise HTTPException(status_code=422, detail=str(error)) from error
                except RepairDependencyChangedError as error:
                    raise HTTPException(status_code=409, detail=str(error)) from error
                except RepairBlockedByGameError as error:
                    raise HTTPException(status_code=409, detail=str(error)) from error
                except BindingPersistenceError:
                    raise HTTPException(
                        status_code=503, detail=_STORAGE_UNAVAILABLE
                    ) from None
                response = RepairConfirmResultResponse.model_validate(result)
                # 目标哈希已在 span 打开前按探针真实范围确定，不再在成功后
                # 动态覆盖，避免掩盖早期 started/failed 的错误目标。
                audit.metadata.update(
                    {
                        "scope": response.scope,
                        "version": response.version,
                        "expected_count": response.expected_count,
                        "cleared_count": response.cleared_count,
                        "result_code": "cleared",
                    }
                )
                return response
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(
                status_code=500,
                detail="审计记录暂不可用",
            ) from error

    for route in router.routes:
        app.router.routes.append(route)
    app.openapi_schema = None
    app.state.komari_character_binding_repair_api_registered = True
