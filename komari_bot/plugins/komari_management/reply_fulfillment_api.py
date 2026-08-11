"""回复履约受审计管理 API（列表 / 详情 / 送达对账 / 承诺续跑）。

只经 komari_chat 顶层窄 seam 获取履约运维服务，管理插件不构造
Repository、不 deep import services / repositories / handlers。
写操作严格要求 ``X-Komari-Change-Reason`` 与显式 ``X-Request-ID``；
审计只记录 operator、request ID、动作、目标哈希、原因与安全结果，
绝不记录原始履约 ID、群 ID、平台消息 ID、正文或异常正文。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol, cast

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field

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
from komari_bot.plugins import komari_chat as chat_plugin

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from komari_bot.management.management_api import ManagementTokenSource
    from komari_bot.management.management_audit import ManagementAuditRecorder

# 运维契约异常只经 komari_chat 顶层暴露面获取（ADR-0006），管理插件
# 不得 import 任意 komari_chat.* 子模块。
ReplyFulfillmentOpsConflictError = chat_plugin.ReplyFulfillmentOpsConflictError
ReplyFulfillmentOpsNotFoundError = chat_plugin.ReplyFulfillmentOpsNotFoundError
ReplyFulfillmentOpsValidationError = chat_plugin.ReplyFulfillmentOpsValidationError

API_PREFIX = "/api/v2/reply-fulfillments"

# 派生状态的固定集合（与 komari_chat 领域推导保持一致）。
ReplyFulfillmentStatusValue = Literal[
    "not_started",
    "pending_confirmation",
    "processing",
    "needs_disposition",
    "completed",
    "not_delivered",
]


class ReplyFulfillmentOpsProtocol(Protocol):
    """履约运维服务的窄接口形状；管理插件只依赖此契约。"""

    async def list_fulfillments(
        self,
        *,
        status: str | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]: ...

    async def get_fulfillment(self, fulfillment_id: str) -> dict[str, Any] | None: ...

    async def confirm_delivered(
        self,
        fulfillment_id: str,
        *,
        platform_message_id: str | None,
    ) -> dict[str, Any]: ...

    async def confirm_not_delivered(self, fulfillment_id: str) -> dict[str, Any]: ...

    async def resume_commitment(
        self,
        fulfillment_id: str,
        *,
        commitment_type: str,
    ) -> dict[str, Any]: ...


class ReplyFulfillmentCommitmentSummary(BaseModel):
    """承诺最小事实：类型、状态、次数、退避时间与稳定错误码。"""

    model_config = ConfigDict(extra="ignore")

    commitment_type: str
    state: str
    attempt_count: int
    next_retry_at: str | None = None
    last_error_code: str | None = None
    completed_at: str | None = None


class ReplyFulfillmentSummary(BaseModel):
    """回复履约最小身份与派生状态（列表项，不含正文与任何载荷）。"""

    model_config = ConfigDict(extra="ignore")

    fulfillment_id: str
    request_trace_id: str
    trigger_message_id: str
    group_id: str
    status: ReplyFulfillmentStatusValue
    reply_fingerprint: str
    prepared_at: str | None = None
    send_started_at: str | None = None
    delivered_at: str | None = None
    platform_message_id: str | None = None
    not_delivered_at: str | None = None
    completed_at: str | None = None
    commitments: list[ReplyFulfillmentCommitmentSummary] = Field(default_factory=list)


class ReplyFulfillmentDetail(ReplyFulfillmentSummary):
    """详情：只有待确认送达才携带核对正文，其他状态为 None。"""

    reply_target_message_id: str | None = None
    reply_content: str | None = None


class ReplyFulfillmentListResponse(BaseModel):
    """列表响应。"""

    items: list[ReplyFulfillmentSummary]
    total: int
    limit: int
    offset: int


class ConfirmDeliveredRequest(BaseModel):
    """确认送达请求；首次无平台消息 ID 允许。"""

    model_config = ConfigDict(extra="forbid")

    platform_message_id: str | None = None


class ConfirmDeliveredResponse(BaseModel):
    """确认送达响应。"""

    fulfillment_id: str
    status: str
    idempotent_replay: bool
    platform_message_id: str | None = None


class ConfirmNotDeliveredResponse(BaseModel):
    """确认未送达响应。"""

    idempotent_replay: bool
    reservation_released: bool


class ResumeCommitmentResponse(BaseModel):
    """承诺续跑响应。"""

    fulfillment_id: str
    status: str
    commitment_type: str
    state: str


def get_reply_fulfillment_ops_service() -> object | None:
    """经 komari_chat 顶层窄 seam 获取履约运维服务（不 deep import）。"""
    return chat_plugin.get_reply_fulfillment_ops_service()


def create_reply_fulfillment_router(
    *,
    api_token: ManagementTokenSource,
    service_getter: Callable[[], object | None],
    audit_recorder: ManagementAuditRecorder | None = None,
) -> APIRouter:
    """创建回复履约对账路由（固定 5 个路由）。"""
    read_auth = create_bearer_auth_dependency(
        api_token,
        detail="未授权访问回复履约对账接口",
        required_permission="reply_fulfillment:read",
    )
    manage_auth = create_bearer_auth_dependency(
        api_token,
        detail="未授权处置回复履约",
        required_permission="reply_fulfillment:manage",
    )
    recorder = audit_recorder or record_management_audit_event
    router = APIRouter(
        prefix=API_PREFIX,
        dependencies=[Depends(read_auth)],
        tags=["reply-fulfillments"],
    )

    def _require_service() -> ReplyFulfillmentOpsProtocol:
        service = service_getter()
        if service is None:
            raise HTTPException(
                status_code=503,
                detail="回复履约运维服务未就绪",
            )
        return cast("ReplyFulfillmentOpsProtocol", service)

    async def _run_ops(operation: Awaitable[dict[str, Any]]) -> dict[str, Any]:
        """把运维失败映射为安全 HTTP 状态码，不回传异常正文。"""
        try:
            return await operation
        except ReplyFulfillmentOpsConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail="回复履约状态冲突",
            ) from exc
        except ReplyFulfillmentOpsNotFoundError as exc:
            raise HTTPException(status_code=404, detail="回复履约不存在") from exc
        except ReplyFulfillmentOpsValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail="回复履约处置请求不合法",
            ) from exc

    @router.get("/fulfillments", response_model=ReplyFulfillmentListResponse)
    async def list_fulfillments(
        status: Annotated[ReplyFulfillmentStatusValue | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> ReplyFulfillmentListResponse:
        """分页列出履约最小身份与派生状态；不返回任何正文或载荷。"""
        service = _require_service()
        result = await service.list_fulfillments(
            status=status,
            limit=limit,
            offset=offset,
        )
        return ReplyFulfillmentListResponse.model_validate(result)

    @router.get(
        "/fulfillments/{fulfillment_id}",
        response_model=ReplyFulfillmentDetail,
    )
    async def get_fulfillment(
        fulfillment_id: Annotated[str, Path()],
    ) -> ReplyFulfillmentDetail:
        """获取单个履约详情；只有待确认送达返回核对正文。"""
        service = _require_service()
        detail = await service.get_fulfillment(fulfillment_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="回复履约不存在")
        return ReplyFulfillmentDetail.model_validate(detail)

    @router.post(
        "/fulfillments/{fulfillment_id}/confirm-delivered",
        response_model=ConfirmDeliveredResponse,
    )
    async def confirm_delivered(
        fulfillment_id: Annotated[str, Path()],
        payload: Annotated[ConfirmDeliveredRequest | None, Body()] = None,
        principal: ManagementPrincipal = Depends(manage_auth),  # noqa: FAST002
        reason: str = Depends(require_management_change_reason),  # noqa: FAST002
        request_id: str = Depends(require_management_request_id),  # noqa: FAST002
    ) -> ConfirmDeliveredResponse:
        """确认送达：先原子持久化送达事实，不同步批量执行承诺。"""
        target_hash = hash_management_target(fulfillment_id)
        async with management_audit_span(
            principal=principal,
            request_id=request_id,
            reason=reason,
            action="reply_fulfillment.confirm_delivered",
            resource="reply_fulfillment",
            target_hash=target_hash,
            recorder=recorder,
        ) as audit:
            service = _require_service()
            platform_message_id = (
                payload.platform_message_id if payload is not None else None
            )
            result = await _run_ops(
                service.confirm_delivered(
                    fulfillment_id,
                    platform_message_id=platform_message_id,
                )
            )
            audit.metadata["idempotent_replay"] = bool(result["idempotent_replay"])
            return ConfirmDeliveredResponse.model_validate(result)

    @router.post(
        "/fulfillments/{fulfillment_id}/confirm-not-delivered",
        response_model=ConfirmNotDeliveredResponse,
    )
    async def confirm_not_delivered(
        fulfillment_id: Annotated[str, Path()],
        principal: ManagementPrincipal = Depends(manage_auth),  # noqa: FAST002
        reason: str = Depends(require_management_change_reason),  # noqa: FAST002
        request_id: str = Depends(require_management_request_id),  # noqa: FAST002
    ) -> ConfirmNotDeliveredResponse:
        """确认未送达：终态落库后幂等释放预占；已送达不可翻案。"""
        target_hash = hash_management_target(fulfillment_id)
        async with management_audit_span(
            principal=principal,
            request_id=request_id,
            reason=reason,
            action="reply_fulfillment.confirm_not_delivered",
            resource="reply_fulfillment",
            target_hash=target_hash,
            recorder=recorder,
        ) as audit:
            service = _require_service()
            result = await _run_ops(service.confirm_not_delivered(fulfillment_id))
            audit.metadata.update(
                {
                    "idempotent_replay": bool(result["idempotent_replay"]),
                    "reservation_released": bool(result["reservation_released"]),
                }
            )
            return ConfirmNotDeliveredResponse.model_validate(result)

    @router.post(
        "/fulfillments/{fulfillment_id}/commitments/{commitment_type}/resume",
        response_model=ResumeCommitmentResponse,
    )
    async def resume_commitment(
        fulfillment_id: Annotated[str, Path()],
        commitment_type: Annotated[str, Path()],
        principal: ManagementPrincipal = Depends(manage_auth),  # noqa: FAST002
        reason: str = Depends(require_management_change_reason),  # noqa: FAST002
        request_id: str = Depends(require_management_request_id),  # noqa: FAST002
    ) -> ResumeCommitmentResponse:
        """续跑指定失败承诺：只接受固定承诺集合，不重新发送回复。"""
        target_hash = hash_management_target(fulfillment_id, commitment_type)
        async with management_audit_span(
            principal=principal,
            request_id=request_id,
            reason=reason,
            action="reply_fulfillment.resume_commitment",
            resource="reply_fulfillment",
            target_hash=target_hash,
            recorder=recorder,
        ) as audit:
            service = _require_service()
            result = await _run_ops(
                service.resume_commitment(
                    fulfillment_id,
                    commitment_type=commitment_type,
                )
            )
            audit.metadata["commitment_type"] = result["commitment_type"]
            return ResumeCommitmentResponse.model_validate(result)

    return router


def register_reply_fulfillment_api(
    app: FastAPI,
    *,
    api_token: ManagementTokenSource,
    allowed_origins: Sequence[str],
    service_getter: Callable[[], object | None],
    audit_recorder: ManagementAuditRecorder | None = None,
) -> None:
    """注册回复履约对账 API（幂等）。"""
    if getattr(app.state, "komari_reply_fulfillment_api_registered", False):
        return

    ensure_management_cors(app, allowed_origins)
    app.include_router(
        create_reply_fulfillment_router(
            api_token=api_token,
            service_getter=service_getter,
            audit_recorder=audit_recorder,
        )
    )
    app.state.komari_reply_fulfillment_api_registered = True
