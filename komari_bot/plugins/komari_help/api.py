"""Komari Help REST API。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Protocol, cast

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Response
from starlette import status

from komari_bot.common.management_api import (
    create_bearer_auth_dependency,
    ensure_management_cors,
)

from .engine import UNSET
from .models import (
    HelpCategory,
    HelpCreateRequest,
    HelpEntry,
    HelpListResponse,
    HelpScanResponse,
    HelpSearchRequest,
    HelpSearchResult,
    HelpUpdateRequest,
)
from .scanner import scan_and_sync

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

API_PREFIX = "/api/komari-help/v1"


class HelpEngineProtocol(Protocol):
    async def list_help(
        self,
        *,
        limit: int,
        offset: int,
        query: str | None = None,
        category: HelpCategory | None = None,
    ) -> tuple[list[HelpEntry], int]: ...

    async def get_help(self, hid: int) -> HelpEntry | None: ...

    async def add_help(
        self,
        title: str,
        content: str,
        keywords: list[str],
        category: HelpCategory = "other",
        plugin_name: str | None = None,
        notes: str | None = None,
        *,
        is_auto_generated: bool = False,
    ) -> int: ...

    async def update_help(self, hid: int, **kwargs: object) -> bool: ...

    async def delete_help(self, hid: int) -> bool: ...

    async def search(
        self,
        query: str,
        limit: int | None = None,
        query_vec: list[float] | None = None,
    ) -> list[HelpSearchResult]: ...


def _validation_error(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=message
    )


def _not_found(hid: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"未找到 ID={hid} 的帮助条目",
    )


def _engine_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Komari Help 引擎未初始化或数据库不可用",
    )


def _build_engine_dependency(
    engine_getter: Callable[[], HelpEngineProtocol | None],
) -> Callable[[], HelpEngineProtocol]:
    def _get_engine() -> HelpEngineProtocol:
        engine = engine_getter()
        if engine is None:
            raise _engine_unavailable()
        return engine

    return _get_engine


def _resolve_update_params(payload: HelpUpdateRequest) -> dict[str, Any]:
    fields_set = payload.model_fields_set
    if not fields_set:
        raise _validation_error("至少提供一个要更新的字段")

    return {
        "title": payload.title if "title" in fields_set else UNSET,
        "content": payload.content if "content" in fields_set else UNSET,
        "keywords": payload.keywords if "keywords" in fields_set else UNSET,
        "category": payload.category if "category" in fields_set else UNSET,
        "plugin_name": payload.plugin_name if "plugin_name" in fields_set else UNSET,
        "notes": payload.notes if "notes" in fields_set else UNSET,
    }


def create_help_router(
    *,
    api_token: str,
    engine_getter: Callable[[], HelpEngineProtocol | None],
) -> APIRouter:
    auth_dependency = create_bearer_auth_dependency(
        api_token,
        detail="未授权访问 Komari Help 管理接口",
    )
    engine_dependency = _build_engine_dependency(engine_getter)
    router = APIRouter(
        prefix=API_PREFIX,
        dependencies=[Depends(auth_dependency)],
        tags=["komari-help"],
    )

    @router.get("/help", response_model=HelpListResponse)
    async def list_help(
        engine: HelpEngineProtocol = Depends(engine_dependency),  # noqa: FAST002
        q: Annotated[str | None, Query(min_length=1)] = None,
        category: HelpCategory | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> HelpListResponse:
        items, total = await engine.list_help(
            limit=limit,
            offset=offset,
            query=q,
            category=category,
        )
        return HelpListResponse(items=items, total=total, limit=limit, offset=offset)

    @router.get("/help/{hid}", response_model=HelpEntry)
    async def get_help(
        hid: int,
        engine: HelpEngineProtocol = Depends(engine_dependency),  # noqa: FAST002
    ) -> HelpEntry:
        item = await engine.get_help(hid)
        if item is None:
            raise _not_found(hid)
        return item

    @router.post("/help", response_model=HelpEntry, status_code=status.HTTP_201_CREATED)
    async def create_help(
        payload: HelpCreateRequest,
        engine: HelpEngineProtocol = Depends(engine_dependency),  # noqa: FAST002
    ) -> HelpEntry:
        hid = await engine.add_help(
            title=payload.title,
            content=payload.content,
            keywords=payload.keywords,
            category=payload.category,
            plugin_name=payload.plugin_name,
            notes=payload.notes,
        )
        item = await engine.get_help(hid)
        if item is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"帮助条目 ID={hid} 创建成功但读取失败",
            )
        return item

    @router.patch("/help/{hid}", response_model=HelpEntry)
    async def update_help(
        hid: int,
        payload: HelpUpdateRequest,
        engine: HelpEngineProtocol = Depends(engine_dependency),  # noqa: FAST002
    ) -> HelpEntry:
        success = await engine.update_help(hid, **_resolve_update_params(payload))
        if not success:
            raise _not_found(hid)
        item = await engine.get_help(hid)
        if item is None:
            raise _not_found(hid)
        return item

    @router.delete(
        "/help/{hid}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response
    )
    async def delete_help(
        hid: int,
        engine: HelpEngineProtocol = Depends(engine_dependency),  # noqa: FAST002
    ) -> Response:
        deleted = await engine.delete_help(hid)
        if not deleted:
            raise _not_found(hid)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/search", response_model=list[HelpSearchResult])
    async def search_help(
        payload: HelpSearchRequest,
        engine: HelpEngineProtocol = Depends(engine_dependency),  # noqa: FAST002
    ) -> list[HelpSearchResult]:
        return await engine.search(payload.query, limit=payload.limit)

    @router.post("/scan", response_model=HelpScanResponse)
    async def scan_help(
        engine: HelpEngineProtocol = Depends(engine_dependency),  # noqa: FAST002
    ) -> HelpScanResponse:
        updated_count = await scan_and_sync(cast("Any", engine))
        return HelpScanResponse(updated_count=updated_count)

    return router


def register_help_api(
    app: FastAPI,
    *,
    api_token: str,
    allowed_origins: Sequence[str],
    engine_getter: Callable[[], HelpEngineProtocol | None],
) -> None:
    if getattr(app.state, "komari_help_api_registered", False):
        return

    ensure_management_cors(app, allowed_origins)
    app.include_router(
        create_help_router(
            api_token=api_token,
            engine_getter=engine_getter,
        )
    )
    app.state.komari_help_api_registered = True
