"""TSK-182 判定诊断管理 REST 路由：健康快照与受权日志查询。

冻结的四个只读端点（TSK-180 §9）：

* ``GET /api/v2/komari-decision/health``                        -- ``decision:read``
* ``GET /api/v2/komari-decision/decisions``                     -- ``decision:read``
* ``GET /api/v2/komari-decision/decisions/{decision_id}``       -- ``decision:read``
* ``GET /api/v2/komari-decision/decisions/{decision_id}/body``  -- ``decision_logs:read``

边界：

- 健康查询只读取调用方提供的被动快照，只调用一次，不做数据库、网络或模型访问；
  没有观测证据的字段保持 ``null``。
- 元数据投影永不包含正文或预览；正文只能经独立权限读取，且每次受权尝试
  （含 404/503）都写入既有管理审计，审计起始失败即拒绝读取。
- 成功与错误响应（401/403/404/422/503）一律 ``Cache-Control: no-store``，且只
  作用于本模块创建的路由表，不改变应用级异常处理器或其它路由语义。
- 错误文案固定，绝不回显异常 ``str``/``repr``、SQL 或调用方输入。
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime  # noqa: TC003 -- Pydantic 运行时求值注解
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel
from starlette import status as http_status
from starlette.exceptions import HTTPException as StarletteHTTPException

from komari_bot.decision.jev import (  # noqa: TC001 -- Pydantic 运行时求值注解
    BusinessRoute,
    DecisionHealthSnapshot,
    DecisionLogMetadata,
    DecisionReason,
    DecisionStatus,
    ReplyEligibility,
    RetentionPurpose,
)
from komari_bot.management.management_api import (
    ManagementPrincipal,
    create_bearer_auth_dependency,
    ensure_management_cors,
)
from komari_bot.management.management_audit import (
    hash_management_target,
    management_audit_span,
    record_management_audit_event,
    resolve_management_request_id,
)
from komari_bot.plugins.komari_decision.repositories.decision_log_repository import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    DecisionLogCorruptRowError,
    DecisionLogRepository,
    DecisionLogRequestError,
    DecisionLogRetentionError,
    DecisionLogStorageUnavailable,
)
from komari_bot.plugins.komari_decision.services.jev_runtime import (
    get_decision_runtime,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Sequence

    from komari_bot.management.management_api import ManagementTokenSource
    from komari_bot.management.management_audit import (
        ManagementAuditRecorder,
    )

API_PREFIX: Final = "/api/v2/komari-decision"
DECISION_BODY_PERMISSION: Final = "decision_logs:read"
DECISION_READ_PERMISSION: Final = "decision:read"
BODY_READ_ACTION: Final = "decision_logs.read"
BODY_READ_REASON: Final = "diagnostic_body_read"
DECISION_RESOURCE: Final = "komari_decision"
STORAGE_UNAVAILABLE_DETAIL: Final = "判定日志存储暂不可用"
"""存储/保留期不可用的唯一对外文案（不区分内部失败原因）。"""

_READ_AUTH_DETAIL: Final = "未授权或无权读取判定元数据"
_BODY_AUTH_DETAIL: Final = "未授权或无权读取判定日志正文"
_NOT_FOUND_DETAIL: Final = "判定日志不存在或已清理"
_INVALID_IDENTIFIER_DETAIL: Final = "判定日志标识必须是 UUID"
_INVALID_QUERY_DETAIL: Final = "判定日志查询参数无效"
_HEALTH_UNAVAILABLE_DETAIL: Final = "判定健康快照暂不可用"

_REGISTRATION_FLAG: Final = "_komari_decision_api_registered"
"""应用状态上的幂等挂载标记（重复注册不重复路由 / CORS 中间件）。"""


# ------------------------------ 响应投影 ------------------------------


class DecisionHealthResponse(BaseModel):
    """被动健康快照的精确字段；没有观测证据的字段保持 ``null``。"""

    runtime_status: Literal["ready", "failed"]
    reason_code: DecisionReason
    last_request_status: Literal["unknown", "success", "failure"]
    last_success_at: datetime | None
    last_failure_at: datetime | None
    last_failure_reason_code: DecisionReason | None
    consecutive_failures: int
    in_flight_count: int | None
    queued_count: int | None
    observed_at: datetime


class _ReplyChoicePayload(BaseModel):
    value: ReplyEligibility
    confidence: float
    probabilities: list[tuple[ReplyEligibility, float]]


class _RetentionChoicePayload(BaseModel):
    value: RetentionPurpose
    confidence: float
    probabilities: list[tuple[RetentionPurpose, float]]


class _ReferenceSummaryPayload(BaseModel):
    complete: bool
    resolved_count: int
    missing_count: int
    cycle_count: int
    timed_out: bool


class _PhaseDurationsPayload(BaseModel):
    queue_ms: float | None
    references_ms: float | None
    request_ms: float | None


class _UsagePayload(BaseModel):
    input_tokens: int | None
    output_tokens: int | None


class _ResultPayload(BaseModel):
    """判定结果元数据投影：只有分类事实，没有正文、提示词或错误原文。"""

    decision_id: str
    status: DecisionStatus
    reason_code: DecisionReason
    route: BusinessRoute
    force_reply: bool
    reply_choice: _ReplyChoicePayload | None
    retention_choice: _RetentionChoicePayload | None
    summary_probability: float | None
    effective_reply: ReplyEligibility | None
    retain_recent: bool | None
    memory_candidate: bool | None
    optional_selected: bool | None
    references: _ReferenceSummaryPayload
    durations: _PhaseDurationsPayload
    usage: _UsagePayload
    config_revision: int | None
    prompt_revision: int | None
    requested_model: str | None
    actual_model: str | None


class _EventIdentityPayload(BaseModel):
    protocol: str
    account_id: str
    group_id: str
    message_id: str


class DecisionMetadataItem(BaseModel):
    """一条判定日志的元数据投影（不含正文/预览）。"""

    decision_id: str
    event: _EventIdentityPayload
    created_at: datetime
    finished_at: datetime
    result: _ResultPayload


class DecisionMetadataPage(BaseModel):
    """元数据分页响应。"""

    items: list[DecisionMetadataItem]
    total: int
    page: int
    page_size: int


class DecisionBodyResponse(BaseModel):
    """受权正文响应；``body`` 为 ``null`` 表示该行未捕获正文。"""

    decision_id: str
    body: dict[str, Any] | None


def _metadata_item(metadata: DecisionLogMetadata) -> DecisionMetadataItem:
    return DecisionMetadataItem(
        decision_id=metadata.decision_id,
        event=_EventIdentityPayload.model_validate(asdict(metadata.event)),
        created_at=metadata.created_at,
        finished_at=metadata.finished_at,
        result=_ResultPayload.model_validate(asdict(metadata.result)),
    )


# ------------------------------ 固定错误 ------------------------------


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=STORAGE_UNAVAILABLE_DETAIL,
    )


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_404_NOT_FOUND,
        detail=_NOT_FOUND_DETAIL,
    )


def _invalid_identifier() -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=_INVALID_IDENTIFIER_DETAIL,
    )


def _invalid_query() -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=_INVALID_QUERY_DETAIL,
    )


def _health_unavailable() -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=_HEALTH_UNAVAILABLE_DETAIL,
    )


def _validated_decision_id(raw: str) -> str:
    """查询前校验 UUID；非法标识返回固定 422，绝不回显输入。"""
    try:
        return str(UUID(raw))
    except (ValueError, AttributeError, TypeError):
        raise _invalid_identifier() from None


def _raise_not_found() -> NoReturn:
    """在审计 span 内抛出固定 404（保持“失败尝试也被审计”的语义）。"""
    raise _not_found()


def _is_storage_failure(error: Exception) -> bool:
    return isinstance(
        error,
        (
            DecisionLogStorageUnavailable,
            DecisionLogRetentionError,
            DecisionLogCorruptRowError,
        ),
    )


def _validated_time_window(
    created_from: datetime | None, created_before: datetime | None
) -> None:
    """HTTP 边界自身保证时间条件合法（带时区、区间有序）。

    仓储对同一规则也做校验；这里再校验一次，使 422 不依赖存储层实现。
    """
    for value in (created_from, created_before):
        if value is not None and value.utcoffset() is None:
            raise _invalid_query()
    if (
        created_from is not None
        and created_before is not None
        and created_from >= created_before
    ):
        raise _invalid_query()


# ------------------------------ 禁止缓存路由 ------------------------------


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


class _NoStoreRoute(APIRoute):
    """判定诊断路由的成功与错误响应一律禁止缓存。

    只在本模块创建的路由表内生效：默认异常处理器输出的响应没有机会附加响应
    头，因此这里就地渲染等价响应，而不改动应用级异常语义。
    """

    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def _no_store_handler(request: Request) -> Response:
            try:
                response = await handler(request)
            except StarletteHTTPException as error:
                return _no_store(
                    JSONResponse(
                        {"detail": error.detail},
                        status_code=error.status_code,
                        headers=dict(error.headers or {}),
                    )
                )
            except RequestValidationError:
                return _no_store(
                    JSONResponse(
                        {"detail": _INVALID_QUERY_DETAIL},
                        status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
                    )
                )
            return _no_store(response)

        return _no_store_handler


# ------------------------------ 路由工厂 ------------------------------


def create_decision_router(
    *,
    api_token: ManagementTokenSource,
    health_provider: Callable[[], DecisionHealthSnapshot],
    log_repository: DecisionLogRepository,
    audit_recorder: ManagementAuditRecorder | None = None,
) -> APIRouter:
    """构造判定诊断只读路由；生产注册由主票统一完成。"""
    read_auth = create_bearer_auth_dependency(
        api_token,
        detail=_READ_AUTH_DETAIL,
        required_permission=DECISION_READ_PERMISSION,
    )
    body_auth = create_bearer_auth_dependency(
        api_token,
        detail=_BODY_AUTH_DETAIL,
        required_permission=DECISION_BODY_PERMISSION,
    )
    recorder = audit_recorder or record_management_audit_event
    router = APIRouter(
        prefix=API_PREFIX,
        tags=["komari-decision"],
        route_class=_NoStoreRoute,
    )

    @router.get(
        "/health",
        response_model=DecisionHealthResponse,
        dependencies=[Depends(read_auth)],
    )
    async def decision_health() -> DecisionHealthResponse:
        """只读取一次被动快照；不访问数据库、网络或模型。"""
        try:
            snapshot = health_provider()
            return DecisionHealthResponse.model_validate(asdict(snapshot))
        except Exception:
            raise _health_unavailable() from None

    @router.get(
        "/decisions",
        response_model=DecisionMetadataPage,
        dependencies=[Depends(read_auth)],
    )
    async def list_decisions(
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
        created_from: Annotated[datetime | None, Query()] = None,
        created_before: Annotated[datetime | None, Query()] = None,
        status: Annotated[DecisionStatus | None, Query()] = None,
        model: Annotated[str | None, Query()] = None,
    ) -> DecisionMetadataPage:
        """分页元数据查询；永不返回正文或预览。"""
        _validated_time_window(created_from, created_before)
        try:
            result = await log_repository.list_metadata(
                page=page,
                page_size=page_size,
                created_from=created_from,
                created_before=created_before,
                status=status,
                model=model,
            )
        except DecisionLogRequestError:
            raise _invalid_query() from None
        except Exception as error:
            if not _is_storage_failure(error):
                raise
            raise _unavailable() from None
        return DecisionMetadataPage(
            items=[_metadata_item(item) for item in result.items],
            total=result.total,
            page=result.page,
            page_size=result.page_size,
        )

    @router.get(
        "/decisions/{decision_id}",
        response_model=DecisionMetadataItem,
        dependencies=[Depends(read_auth)],
    )
    async def decision_detail(decision_id: str) -> DecisionMetadataItem:
        """单条元数据；不存在或已超期返回 404，绝不伪造空元数据。"""
        normalized = _validated_decision_id(decision_id)
        try:
            metadata = await log_repository.get_metadata(normalized)
        except DecisionLogRequestError:
            raise _invalid_query() from None
        except Exception as error:
            if not _is_storage_failure(error):
                raise
            raise _unavailable() from None
        if metadata is None:
            raise _not_found()
        return _metadata_item(metadata)

    @router.get(
        "/decisions/{decision_id}/body",
        response_model=DecisionBodyResponse,
    )
    async def decision_body(
        decision_id: str,
        principal: ManagementPrincipal = Depends(body_auth),  # noqa: FAST002
        request_id: str = Depends(  # noqa: FAST002
            resolve_management_request_id
        ),
    ) -> DecisionBodyResponse:
        """受权正文读取：先起审计再读，读不到只返回固定 404/503。"""
        normalized = _validated_decision_id(decision_id)
        try:
            async with management_audit_span(
                principal=principal,
                request_id=request_id,
                reason=BODY_READ_REASON,
                action=BODY_READ_ACTION,
                resource=DECISION_RESOURCE,
                target_hash=hash_management_target(normalized),
                recorder=recorder,
            ) as audit:
                try:
                    found = await log_repository.get_body(normalized)
                except DecisionLogRequestError:
                    raise _invalid_query() from None
                except Exception as error:
                    if not _is_storage_failure(error):
                        raise
                    raise _unavailable() from None
                if found is None:
                    _raise_not_found()
                audit.metadata.update({"body_present": found.body is not None})
                return DecisionBodyResponse(
                    decision_id=found.decision_id,
                    body=found.body,
                )
        except StarletteHTTPException:
            raise
        except Exception:
            # 审计起始失败（或其它未预期失败）：拒绝正文读取，绝不先读后补审计。
            raise _unavailable() from None

    return router


# ------------------------------ 生产装配入口 ------------------------------


def register_decision_api(
    app: FastAPI,
    *,
    api_token: ManagementTokenSource,
    allowed_origins: Sequence[str],
    audit_recorder: ManagementAuditRecorder | None = None,
) -> None:
    """装配判定诊断只读管理面（幂等）。

    签名与其它插件的管理装配入口一致：``(app, *, api_token, allowed_origins,
    audit_recorder=None)``。健康快照与判定日志仓储都取自插件内部的**同一个**
    常驻运行时实例（与引擎共享同一个仓储），因此判定关闭或初始化失败时路由仍在，
    并按既有 503/404 语义回答；不授予任何新权限，不改变既有鉴权、正文审计、禁止
    缓存与保留期行为。
    """
    if getattr(app.state, _REGISTRATION_FLAG, False):
        return

    runtime = get_decision_runtime()
    ensure_management_cors(app, list(allowed_origins))
    app.include_router(
        create_decision_router(
            api_token=api_token,
            health_provider=runtime.get_health,
            log_repository=runtime.log_repository,
            audit_recorder=audit_recorder,
        )
    )
    setattr(app.state, _REGISTRATION_FLAG, True)


__all__ = [
    "API_PREFIX",
    "BODY_READ_ACTION",
    "BODY_READ_REASON",
    "DECISION_BODY_PERMISSION",
    "DECISION_READ_PERMISSION",
    "DECISION_RESOURCE",
    "STORAGE_UNAVAILABLE_DETAIL",
    "DecisionBodyResponse",
    "DecisionHealthResponse",
    "DecisionMetadataItem",
    "DecisionMetadataPage",
    "create_decision_router",
    "register_decision_api",
]
