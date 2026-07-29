"""Agent Run 日志 REST API。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field
from starlette import status

from komari_bot.common.management_api import (
    create_bearer_auth_dependency,
    ensure_management_cors,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from komari_bot.common.management_api import ManagementTokenSource

API_PREFIX = "/api/v2/agent-run-logs"


class AgentRunLogReaderProtocol(Protocol):
    async def list_runs(
        self,
        *,
        log_date: str | None = None,
        days: int = 7,
        run_type: str | None = None,
        task_kind: str | None = None,
        origin: str | None = None,
        trace_id: str | None = None,
        status: str | None = None,
        model: str | None = None,
        method: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]: ...

    async def get_run(self, run_id: str) -> dict[str, Any] | None: ...


class AgentRunListItem(BaseModel):
    """当前页从 JSONL 临时生成正文预览的任务摘要。"""

    run_id: str
    trace_id: str
    date: str
    run_type: str
    task_kind: str
    origin: Literal["normal", "debug"]
    status: Literal["success", "error", "cancelled"]
    started_at: str
    finished_at: str
    duration_ms: float | None = None
    models: list[str] = Field(default_factory=list)
    methods: list[str] = Field(default_factory=list)
    round_count: int = 0
    tool_count: int = 0
    usage: dict[str, Any] = Field(default_factory=dict)
    input_preview: str = ""
    output_preview: str = ""
    error_preview: str = ""


class AgentRunListResponse(BaseModel):
    items: list[AgentRunListItem]
    total: int
    limit: int
    offset: int


class AgentRunDetail(BaseModel):
    """权威 JSONL v3 完整任务日志。"""

    model_config = {"extra": "allow"}

    schema_version: int = 3
    run_id: str
    trace_id: str
    run_type: str
    task_kind: str
    origin: str
    status: str
    started_at: str
    finished_at: str
    duration_ms: float
    input: Any = None
    output: Any = None
    error: Any = None
    rounds: list[dict[str, Any]] = Field(default_factory=list)
    tool_executions: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    models: list[str] = Field(default_factory=list)
    methods: list[str] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _run_not_found(run_id: str) -> HTTPException:
    return _not_found(f"未找到 Agent Run 日志: {run_id}")


def _reader_dependency(
    reader_getter: Callable[[], AgentRunLogReaderProtocol | None],
) -> Callable[[], AgentRunLogReaderProtocol]:
    def get_reader() -> AgentRunLogReaderProtocol:
        reader = reader_getter()
        if reader is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Agent Run 日志读取器未初始化",
            )
        return reader

    return get_reader


def create_agent_run_log_router(
    *,
    api_token: ManagementTokenSource,
    reader_getter: Callable[[], AgentRunLogReaderProtocol | None],
) -> APIRouter:
    auth_dependency = create_bearer_auth_dependency(
        api_token,
        detail="未授权访问 Agent Run 日志接口",
        required_permission="agent_run_logs:read",
    )
    get_reader = _reader_dependency(reader_getter)
    router = APIRouter(
        prefix=API_PREFIX,
        dependencies=[Depends(auth_dependency)],
        tags=["agent-run-logs"],
    )

    @router.get("/runs", response_model=AgentRunListResponse)
    async def list_runs(
        reader: AgentRunLogReaderProtocol = Depends(get_reader),  # noqa: FAST002
        log_date: Annotated[
            str | None,
            Query(alias="date", pattern=r"^\d{4}-\d{2}-\d{2}$"),
        ] = None,
        days: Annotated[int, Query(ge=1, le=90)] = 7,
        run_type: Annotated[str | None, Query(min_length=1)] = None,
        task_kind: Annotated[str | None, Query(min_length=1)] = None,
        origin: Literal["normal", "debug"] | None = None,
        trace_id: Annotated[str | None, Query(min_length=1)] = None,
        run_status: Annotated[
            Literal["success", "error", "cancelled"] | None,
            Query(alias="status"),
        ] = None,
        model: Annotated[str | None, Query(min_length=1)] = None,
        method: Annotated[str | None, Query(min_length=1)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> AgentRunListResponse:
        try:
            items, total = await reader.list_runs(
                log_date=log_date,
                days=days,
                run_type=run_type,
                task_kind=task_kind,
                origin=origin,
                trace_id=trace_id,
                status=run_status,
                model=model,
                method=method,
                limit=limit,
                offset=offset,
            )
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(error),
            ) from error
        return AgentRunListResponse(
            items=[AgentRunListItem.model_validate(item) for item in items],
            total=total,
            limit=limit,
            offset=offset,
        )

    @router.get("/runs/{run_id}", response_model=AgentRunDetail)
    async def get_run(
        run_id: Annotated[str, Path(min_length=1)],
        reader: AgentRunLogReaderProtocol = Depends(get_reader),  # noqa: FAST002
    ) -> AgentRunDetail:
        item = await reader.get_run(run_id)
        if item is None:
            raise _run_not_found(run_id)
        return AgentRunDetail.model_validate(item)

    return router


def register_agent_run_log_api(
    app: FastAPI,
    *,
    api_token: ManagementTokenSource,
    allowed_origins: Sequence[str],
    reader_getter: Callable[[], AgentRunLogReaderProtocol | None],
) -> None:
    if getattr(app.state, "komari_agent_run_log_api_registered", False):
        return
    ensure_management_cors(app, allowed_origins)
    app.include_router(
        create_agent_run_log_router(
            api_token=api_token,
            reader_getter=reader_getter,
        )
    )
    app.state.komari_agent_run_log_api_registered = True


__all__ = [
    "API_PREFIX",
    "AgentRunDetail",
    "AgentRunListItem",
    "AgentRunListResponse",
    "create_agent_run_log_router",
    "register_agent_run_log_api",
]
