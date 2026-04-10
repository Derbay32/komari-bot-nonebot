"""Komari Knowledge REST API。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Protocol

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Response
from starlette import status

from komari_bot.common.management_api import (
    create_bearer_auth_dependency,
    ensure_management_cors,
)

from .engine import UNSET
from .models import (
    KnowledgeCategory,
    KnowledgeCreateRequest,
    KnowledgeEntry,
    KnowledgeListResponse,
    KnowledgeSearchHit,
    KnowledgeSearchRequest,
    KnowledgeUpdateRequest,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

API_PREFIX = "/api/komari-knowledge/v1"


class KnowledgeEngineProtocol(Protocol):
    """REST API 需要的最小引擎协议。"""

    async def list_knowledge(
        self,
        *,
        limit: int,
        offset: int,
        query: str | None = None,
        category: KnowledgeCategory | None = None,
    ) -> tuple[list[KnowledgeEntry], int]: ...

    async def get_knowledge(self, kid: int) -> KnowledgeEntry | None: ...

    async def add_knowledge(
        self,
        content: str,
        keywords: list[str],
        category: KnowledgeCategory = "general",
        notes: str | None = None,
    ) -> int: ...

    async def update_knowledge(self, kid: int, **kwargs: object) -> bool: ...

    async def delete_knowledge(self, kid: int) -> bool: ...

    async def search(
        self,
        query: str,
        limit: int | None = None,
        query_vec: list[float] | None = None,
    ) -> list[KnowledgeSearchHit]: ...


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="未授权访问 Komari Knowledge 管理接口",
    )


def _validation_error(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=message,
    )


def _not_found(kid: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"未找到 ID={kid} 的知识",
    )


def _engine_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Komari Knowledge 引擎未初始化或数据库不可用",
    )


def _content_required_error() -> HTTPException:
    return _validation_error("content 不能为空")


def _keywords_required_error() -> HTTPException:
    return _validation_error("keywords 不能为空")


def _category_required_error() -> HTTPException:
    return _validation_error("category 不能为空")

def _build_engine_dependency(
    engine_getter: Callable[[], KnowledgeEngineProtocol | None],
) -> Callable[[], KnowledgeEngineProtocol]:
    def _get_engine() -> KnowledgeEngineProtocol:
        engine = engine_getter()
        if engine is None:
            raise _engine_unavailable()
        return engine

    return _get_engine


def _resolve_update_params(payload: KnowledgeUpdateRequest) -> dict[str, Any]:
    fields_set = payload.model_fields_set
    if not fields_set:
        raise _validation_error("至少提供一个要更新的字段")

    if "content" in fields_set and payload.content is None:
        raise _content_required_error()
    if "keywords" in fields_set and payload.keywords is None:
        raise _keywords_required_error()
    if "category" in fields_set and payload.category is None:
        raise _category_required_error()

    return {
        "content": payload.content if "content" in fields_set else UNSET,
        "keywords": payload.keywords if "keywords" in fields_set else UNSET,
        "category": payload.category if "category" in fields_set else UNSET,
        "notes": payload.notes if "notes" in fields_set else UNSET,
    }


def create_knowledge_router(
    *,
    api_token: str,
    engine_getter: Callable[[], KnowledgeEngineProtocol | None],
) -> APIRouter:
    """创建知识库管理路由。"""
    auth_dependency = create_bearer_auth_dependency(
        api_token,
        detail="未授权访问 Komari Knowledge 管理接口",
    )
    engine_dependency = _build_engine_dependency(engine_getter)
    router = APIRouter(
        prefix=API_PREFIX,
        dependencies=[Depends(auth_dependency)],
        tags=["komari-knowledge"],
    )

    @router.get("/knowledge", response_model=KnowledgeListResponse)
    async def list_knowledge(
        engine: KnowledgeEngineProtocol = Depends(engine_dependency),  # noqa: FAST002
        q: Annotated[str | None, Query(min_length=1)] = None,
        category: KnowledgeCategory | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> KnowledgeListResponse:
        items, total = await engine.list_knowledge(
            limit=limit,
            offset=offset,
            query=q,
            category=category,
        )
        return KnowledgeListResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
        )

    @router.get("/knowledge/{kid}", response_model=KnowledgeEntry)
    async def get_knowledge(
        kid: int,
        engine: KnowledgeEngineProtocol = Depends(engine_dependency),  # noqa: FAST002
    ) -> KnowledgeEntry:
        item = await engine.get_knowledge(kid)
        if item is None:
            raise _not_found(kid)
        return item

    @router.post(
        "/knowledge",
        response_model=KnowledgeEntry,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_knowledge(
        payload: KnowledgeCreateRequest,
        engine: KnowledgeEngineProtocol = Depends(engine_dependency),  # noqa: FAST002
    ) -> KnowledgeEntry:
        kid = await engine.add_knowledge(
            content=payload.content,
            keywords=payload.keywords,
            category=payload.category,
            notes=payload.notes,
        )
        item = await engine.get_knowledge(kid)
        if item is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"知识 ID={kid} 创建成功但读取失败",
            )
        return item

    @router.patch("/knowledge/{kid}", response_model=KnowledgeEntry)
    async def update_knowledge(
        kid: int,
        payload: KnowledgeUpdateRequest,
        engine: KnowledgeEngineProtocol = Depends(engine_dependency),  # noqa: FAST002
    ) -> KnowledgeEntry:
        success = await engine.update_knowledge(kid, **_resolve_update_params(payload))
        if not success:
            raise _not_found(kid)
        item = await engine.get_knowledge(kid)
        if item is None:
            raise _not_found(kid)
        return item

    @router.delete(
        "/knowledge/{kid}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    async def delete_knowledge(
        kid: int,
        engine: KnowledgeEngineProtocol = Depends(engine_dependency),  # noqa: FAST002
    ) -> Response:
        deleted = await engine.delete_knowledge(kid)
        if not deleted:
            raise _not_found(kid)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/search", response_model=list[KnowledgeSearchHit])
    async def search_knowledge(
        payload: KnowledgeSearchRequest,
        engine: KnowledgeEngineProtocol = Depends(engine_dependency),  # noqa: FAST002
    ) -> list[KnowledgeSearchHit]:
        results = await engine.search(payload.query, limit=payload.limit)
        return [KnowledgeSearchHit.model_validate(item) for item in results]

    return router


def register_knowledge_api(
    app: FastAPI,
    *,
    api_token: str,
    allowed_origins: Sequence[str],
    engine_getter: Callable[[], KnowledgeEngineProtocol | None],
) -> None:
    """注册知识库 REST API 与可选 CORS。"""
    if getattr(app.state, "komari_knowledge_api_registered", False):
        return

    ensure_management_cors(app, allowed_origins)

    app.include_router(
        create_knowledge_router(
            api_token=api_token,
            engine_getter=engine_getter,
        )
    )
    app.state.komari_knowledge_api_registered = True
