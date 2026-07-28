"""LLM Provider reply 日志 REST API。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Query
from pydantic import BaseModel
from starlette import status

from komari_bot.common.management_api import (
    create_bearer_auth_dependency,
    ensure_management_cors,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

API_PREFIX = "/api/llm-provider/v1"


class ReplyLogReaderProtocol(Protocol):
    """REST API 需要的最小日志读取协议。"""

    async def list_logs(
        self,
        *,
        date: str | None = None,
        days: int = 7,
        trace_id: str | None = None,
        model: str | None = None,
        method: str | None = None,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]: ...

    async def get_log(
        self,
        *,
        date: str,
        line_number: int,
    ) -> dict[str, Any] | None: ...

class ReplyLogListItem(BaseModel):
    """reply 日志摘要。"""

    date: str
    line_number: int
    timestamp: str
    method: str
    model: str
    trace_id: str = ""
    phase: str = ""
    duration_ms: float | None = None
    status: Literal["success", "error"]
    input_preview: str = ""
    output_preview: str = ""
    error_preview: str = ""


class ReplyLogListResponse(BaseModel):
    """reply 日志列表响应。"""

    items: list[ReplyLogListItem]
    total: int
    limit: int
    offset: int


class ReplyLogDetail(ReplyLogListItem):
    """reply 日志详情。"""

    input: Any = None
    output: str | None = None
    error: str | None = None


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


def _reply_log_not_found(date: str, line_number: int) -> HTTPException:
    return _not_found(f"未找到 {date} 第 {line_number} 行的 reply 日志")


def create_llm_provider_router(
    *,
    api_token: str,
    reader_getter: Callable[[], ReplyLogReaderProtocol | None],
) -> APIRouter:
    """创建 llm_provider reply 日志路由。"""
    auth_dependency = create_bearer_auth_dependency(
        api_token,
        detail="未授权访问 LLM Provider 管理接口",
    )

    def _get_reader() -> ReplyLogReaderProtocol:
        reader = reader_getter()
        if reader is None:
            msg = "LLM Provider reply 日志读取器未初始化"
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=msg,
            )
        return reader

    router = APIRouter(
        prefix=API_PREFIX,
        dependencies=[Depends(auth_dependency)],
        tags=["llm-provider"],
    )

    @router.get("/reply-logs", response_model=ReplyLogListResponse)
    async def list_reply_logs(
        reader: ReplyLogReaderProtocol = Depends(_get_reader),  # noqa: FAST002
        date: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")] = None,
        days: Annotated[int, Query(ge=1, le=30)] = 7,
        trace_id: Annotated[str | None, Query(min_length=1)] = None,
        model: Annotated[str | None, Query(min_length=1)] = None,
        method: Annotated[str | None, Query(min_length=1)] = None,
        status: Literal["success", "error"] | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> ReplyLogListResponse:
        try:
            items, total = await reader.list_logs(
                date=date,
                days=days,
                trace_id=trace_id,
                model=model,
                method=method,
                status=status,
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            raise _validation_error(str(exc)) from exc

        return ReplyLogListResponse(
            items=[ReplyLogListItem.model_validate(item) for item in items],
            total=total,
            limit=limit,
            offset=offset,
        )

    @router.get(
        "/reply-logs/{date}/{line_number}",
        response_model=ReplyLogDetail,
    )
    async def get_reply_log(
        date: Annotated[str, Path(pattern=r"^\d{4}-\d{2}-\d{2}$")],
        line_number: Annotated[int, Path(ge=1)],
        reader: ReplyLogReaderProtocol = Depends(_get_reader),  # noqa: FAST002
    ) -> ReplyLogDetail:
        try:
            item = await reader.get_log(date=date, line_number=line_number)
        except ValueError as exc:
            raise _validation_error(str(exc)) from exc
        if item is None:
            raise _reply_log_not_found(date, line_number)
        return ReplyLogDetail.model_validate(item)

    return router


def register_llm_provider_api(
    app: FastAPI,
    *,
    api_token: str,
    allowed_origins: Sequence[str],
    reader_getter: Callable[[], ReplyLogReaderProtocol | None],
) -> None:
    """注册 llm_provider reply 日志 API。"""
    if getattr(app.state, "komari_llm_provider_api_registered", False):
        return

    ensure_management_cors(app, allowed_origins)
    app.include_router(
        create_llm_provider_router(
            api_token=api_token,
            reader_getter=reader_getter,
        )
    )
    app.state.komari_llm_provider_api_registered = True
