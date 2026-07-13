"""用户封禁管理 REST API。"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003
from typing import TYPE_CHECKING, Annotated, Literal

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from komari_bot.common.management_api import (
    create_bearer_auth_dependency,
    ensure_management_cors,
)

from .event_support import is_configured_superuser_id
from .models import (
    BanListPage,
    BanMutationResult,
    BanRecord,
    BanScope,
    BanTargetScope,
    NotificationResult,
    UserBanStatus,
    normalize_ban_reason,
    normalize_qq_user_id,
    parse_ban_duration,
)
from .notifications import (
    get_first_available_bot,
    notify_ban_result,
    notify_unban_result,
)
from .service import BanServiceUnavailableError, UserBanService, get_service

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

API_PREFIX = "/api/komari-user-bans/v1"
MANAGEMENT_API_OPERATOR_ID = "management_api"


class BanRecordResponse(BaseModel):
    """单个作用域的封禁记录响应。"""

    user_id: str
    scope: BanScope
    operator_id: str
    reason: str | None
    expires_at: datetime | None
    permanent: bool
    created_at: datetime
    updated_at: datetime


class UserBanStatusResponse(BaseModel):
    """用户当前封禁状态响应。"""

    user_id: str
    active_scopes: list[BanScope]
    records: list[BanRecordResponse]
    superuser_bypass: bool


class BanListResponse(BaseModel):
    """封禁分页列表响应。"""

    items: list[UserBanStatusResponse]
    total: int
    page: int
    page_size: int


class NotificationResponse(BaseModel):
    """私信通知尝试结果响应。"""

    attempted: bool
    sent: bool
    error: str | None


class BanMutationResponse(BaseModel):
    """封禁或解封修改响应。"""

    changed: bool
    action: Literal["created", "updated", "unchanged", "removed"]
    status: UserBanStatusResponse
    notification: NotificationResponse


class CreateBanRequest(BaseModel):
    """创建或覆盖封禁请求。"""

    model_config = ConfigDict(extra="forbid")

    user_id: StrictStr = Field(description="不带前导零的 QQ 号")
    scope: BanTargetScope
    duration: StrictStr = Field(default="permanent", description="封禁时长")
    reason: StrictStr | None = Field(default=None, description="封禁理由")

    @field_validator("user_id")
    @classmethod
    def validate_user_id(cls, value: str) -> str:
        """校验并规范化 QQ 号。"""
        normalized = normalize_qq_user_id(value)
        if normalized is None:
            msg = "user_id 必须是不带前导零的正整数"
            raise ValueError(msg)
        return normalized

    @field_validator("duration")
    @classmethod
    def validate_duration(cls, value: str) -> str:
        """校验并规范化时长表达式。"""
        normalized = value.strip().lower()
        parse_ban_duration(normalized)
        return normalized

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str | None) -> str | None:
        """校验并规范化封禁理由。"""
        return normalize_ban_reason(value)


def _record_response(record: BanRecord) -> BanRecordResponse:
    return BanRecordResponse(
        user_id=record.user_id,
        scope=record.ban_scope,
        operator_id=record.operator_id,
        reason=record.reason,
        expires_at=record.expires_at,
        permanent=record.is_permanent,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _status_response(status: UserBanStatus) -> UserBanStatusResponse:
    records = status.active_records
    return UserBanStatusResponse(
        user_id=status.user_id,
        active_scopes=sorted(record.ban_scope for record in records),
        records=[_record_response(record) for record in records],
        superuser_bypass=is_configured_superuser_id(status.user_id),
    )


def _notification_response(result: NotificationResult) -> NotificationResponse:
    return NotificationResponse(
        attempted=result.attempted,
        sent=result.sent,
        error=result.error,
    )


def _mutation_response(
    result: BanMutationResult,
    notification: NotificationResult,
) -> BanMutationResponse:
    return BanMutationResponse(
        changed=result.changed,
        action=result.mutation_kind,
        status=_status_response(result.status),
        notification=_notification_response(notification),
    )


def _validated_path_user_id(value: str) -> str:
    user_id = normalize_qq_user_id(value)
    if user_id is None:
        raise HTTPException(
            status_code=422,
            detail="user_id 必须是不带前导零的正整数",
        )
    return user_id


def _storage_error(error: BanServiceUnavailableError) -> HTTPException:
    return HTTPException(status_code=503, detail=str(error))


def create_user_ban_router(
    *,
    api_token: str,
    service_getter: Callable[[], UserBanService] = get_service,
) -> APIRouter:
    """创建用户封禁管理路由。"""
    auth_dependency = create_bearer_auth_dependency(
        api_token,
        detail="未授权访问用户封禁接口",
    )
    router = APIRouter(
        prefix=API_PREFIX,
        dependencies=[Depends(auth_dependency)],
        tags=["komari-user-bans"],
    )

    @router.get("/bans", response_model=BanListResponse)
    async def list_bans(
        scope: Annotated[BanTargetScope, Query()] = "all",
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> BanListResponse:
        """分页查询当前有效的用户封禁。"""
        query_scope: BanScope | None = None if scope == "all" else scope
        try:
            result: BanListPage = await service_getter().list_bans(
                scope=query_scope,
                limit=page_size,
                offset=(page - 1) * page_size,
            )
        except BanServiceUnavailableError as error:
            raise _storage_error(error) from error
        return BanListResponse(
            items=[_status_response(status) for status in result.items],
            total=result.total,
            page=page,
            page_size=page_size,
        )

    @router.get("/bans/{user_id}", response_model=UserBanStatusResponse)
    async def get_ban_status(
        user_id: Annotated[str, Path()],
    ) -> UserBanStatusResponse:
        """查询指定 QQ 用户的当前封禁。"""
        normalized_user_id = _validated_path_user_id(user_id)
        try:
            status = await service_getter().get_status(normalized_user_id)
        except BanServiceUnavailableError as error:
            raise _storage_error(error) from error
        return _status_response(status)

    @router.post("/bans", response_model=BanMutationResponse)
    async def create_or_update_ban(
        payload: Annotated[CreateBanRequest, Body()],
    ) -> BanMutationResponse:
        """创建或覆盖指定用户封禁。"""
        try:
            expires_at = parse_ban_duration(payload.duration)
            result = await service_getter().ban_user(
                user_id=payload.user_id,
                target_scope=payload.scope,
                operator_id=MANAGEMENT_API_OPERATOR_ID,
                expires_at=expires_at,
                reason=payload.reason,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except BanServiceUnavailableError as error:
            raise _storage_error(error) from error

        superuser_bypass = is_configured_superuser_id(payload.user_id)
        notification = await notify_ban_result(
            get_first_available_bot(),
            result,
            superuser_bypass=superuser_bypass,
        )
        return _mutation_response(result, notification)

    @router.delete(
        "/bans/{user_id}/{scope}",
        response_model=BanMutationResponse,
    )
    async def delete_ban(
        user_id: Annotated[str, Path()],
        scope: Annotated[BanTargetScope, Path()],
    ) -> BanMutationResponse:
        """手动解除指定用户封禁。"""
        normalized_user_id = _validated_path_user_id(user_id)
        try:
            result = await service_getter().unban_user(
                user_id=normalized_user_id,
                target_scope=scope,
            )
        except BanServiceUnavailableError as error:
            raise _storage_error(error) from error

        superuser_bypass = is_configured_superuser_id(normalized_user_id)
        notification = await notify_unban_result(
            get_first_available_bot(),
            result,
            superuser_bypass=superuser_bypass,
        )
        return _mutation_response(result, notification)

    return router


def register_user_ban_api(
    app: FastAPI,
    *,
    api_token: str,
    allowed_origins: Sequence[str],
    service_getter: Callable[[], UserBanService] = get_service,
) -> None:
    """在统一管理应用上注册用户封禁 API。"""
    if getattr(app.state, "komari_user_ban_api_registered", False):
        return

    ensure_management_cors(app, allowed_origins)
    app.include_router(
        create_user_ban_router(
            api_token=api_token,
            service_getter=service_getter,
        )
    )
    app.state.komari_user_ban_api_registered = True
