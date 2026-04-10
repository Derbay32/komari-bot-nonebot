"""Komari Memory REST API。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Protocol

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Query, Response
from starlette import status

from komari_bot.common.management_api import (
    create_bearer_auth_dependency,
    ensure_management_cors,
)

from .api_models import (
    ConversationCreateRequest,
    ConversationEntry,
    ConversationListResponse,
    ConversationUpdateRequest,
    MemoryEntityEntry,
    MemoryEntityListResponse,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

API_PREFIX = "/api/komari-memory/v1"


class MemoryServiceProtocol(Protocol):
    """REST API 需要的最小记忆服务协议。"""

    async def list_conversations(
        self,
        *,
        limit: int,
        offset: int,
        group_id: str | None = None,
        participant: str | None = None,
        query: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]: ...

    async def get_conversation_entry(
        self,
        conversation_id: int,
    ) -> dict[str, Any] | None: ...

    async def create_conversation_entry(
        self,
        *,
        group_id: str,
        summary: str,
        participants: list[str],
        importance_initial: int = 3,
        importance_current: float | None = None,
        start_time: object | None = None,
        end_time: object | None = None,
        last_accessed: object | None = None,
    ) -> dict[str, Any]: ...

    async def update_conversation_entry(
        self,
        conversation_id: int,
        *,
        group_id: str | None = None,
        summary: str | None = None,
        participants: list[str] | None = None,
        importance_initial: int | None = None,
        importance_current: float | None = None,
        start_time: object | None = None,
        end_time: object | None = None,
        last_accessed: object | None = None,
    ) -> dict[str, Any] | None: ...

    async def delete_conversation_entry(self, conversation_id: int) -> bool: ...

    async def list_user_profile_rows(
        self,
        *,
        limit: int,
        offset: int,
        group_id: str | None = None,
        user_id: str | None = None,
        query: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]: ...

    async def list_interaction_history_rows(
        self,
        *,
        limit: int,
        offset: int,
        group_id: str | None = None,
        user_id: str | None = None,
        query: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]: ...

    async def get_user_profile_row(
        self,
        *,
        user_id: str,
        group_id: str,
    ) -> dict[str, Any] | None: ...

    async def get_interaction_history_row(
        self,
        *,
        user_id: str,
        group_id: str,
    ) -> dict[str, Any] | None: ...

    async def upsert_user_profile_row(
        self,
        *,
        user_id: str,
        group_id: str,
        profile: dict[str, Any],
        importance: int = 4,
    ) -> dict[str, Any] | None: ...

    async def upsert_interaction_history_row(
        self,
        *,
        user_id: str,
        group_id: str,
        interaction: dict[str, Any],
        importance: int = 5,
    ) -> dict[str, Any] | None: ...

    async def delete_user_profile(
        self,
        *,
        user_id: str,
        group_id: str,
    ) -> bool: ...

    async def delete_interaction_history(
        self,
        *,
        user_id: str,
        group_id: str,
    ) -> bool: ...


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


def _service_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Komari Memory 服务未初始化或数据库不可用",
    )


def _user_id_mismatch_error() -> HTTPException:
    return _validation_error("请求体中的 user_id 与路径参数不一致")


def _conversation_not_found(conversation_id: int) -> HTTPException:
    return _not_found(f"未找到 ID={conversation_id} 的对话记忆")


def _user_profile_not_found(group_id: str, user_id: str) -> HTTPException:
    return _not_found(f"未找到 group={group_id} user={user_id} 的用户画像")


def _interaction_history_not_found(group_id: str, user_id: str) -> HTTPException:
    return _not_found(f"未找到 group={group_id} user={user_id} 的互动历史")


def _build_service_dependency(
    service_getter: Callable[[], MemoryServiceProtocol | None],
) -> Callable[[], MemoryServiceProtocol]:
    def _get_service() -> MemoryServiceProtocol:
        service = service_getter()
        if service is None:
            raise _service_unavailable()
        return service

    return _get_service


def _resolve_conversation_patch_params(
    payload: ConversationUpdateRequest,
) -> dict[str, Any]:
    fields_set = payload.model_fields_set
    if not fields_set:
        raise _validation_error("至少提供一个要更新的字段")

    return {
        "group_id": payload.group_id if "group_id" in fields_set else None,
        "summary": payload.summary if "summary" in fields_set else None,
        "participants": payload.participants if "participants" in fields_set else None,
        "importance_initial": (
            payload.importance_initial if "importance_initial" in fields_set else None
        ),
        "importance_current": (
            payload.importance_current if "importance_current" in fields_set else None
        ),
        "start_time": payload.start_time if "start_time" in fields_set else None,
        "end_time": payload.end_time if "end_time" in fields_set else None,
        "last_accessed": (
            payload.last_accessed if "last_accessed" in fields_set else None
        ),
    }


def _ensure_payload_user_id(payload: dict[str, Any], user_id: str) -> None:
    payload_user_id = payload.get("user_id")
    if payload_user_id is None:
        return
    if str(payload_user_id) != user_id:
        raise _user_id_mismatch_error()


def create_memory_router(
    *,
    api_token: str,
    service_getter: Callable[[], MemoryServiceProtocol | None],
) -> APIRouter:
    """创建记忆库管理路由。"""
    auth_dependency = create_bearer_auth_dependency(
        api_token,
        detail="未授权访问 Komari Memory 管理接口",
    )
    service_dependency = _build_service_dependency(service_getter)
    router = APIRouter(
        prefix=API_PREFIX,
        dependencies=[Depends(auth_dependency)],
        tags=["komari-memory"],
    )

    @router.get("/conversations", response_model=ConversationListResponse)
    async def list_conversations(
        service: MemoryServiceProtocol = Depends(service_dependency),  # noqa: FAST002
        group_id: Annotated[str | None, Query(min_length=1)] = None,
        participant: Annotated[str | None, Query(min_length=1)] = None,
        q: Annotated[str | None, Query(min_length=1)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> ConversationListResponse:
        items, total = await service.list_conversations(
            limit=limit,
            offset=offset,
            group_id=group_id,
            participant=participant,
            query=q,
        )
        return ConversationListResponse(
            items=[ConversationEntry.model_validate(item) for item in items],
            total=total,
            limit=limit,
            offset=offset,
        )

    @router.get("/conversations/{conversation_id}", response_model=ConversationEntry)
    async def get_conversation(
        conversation_id: int,
        service: MemoryServiceProtocol = Depends(service_dependency),  # noqa: FAST002
    ) -> ConversationEntry:
        item = await service.get_conversation_entry(conversation_id)
        if item is None:
            raise _conversation_not_found(conversation_id)
        return ConversationEntry.model_validate(item)

    @router.post(
        "/conversations",
        response_model=ConversationEntry,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_conversation(
        payload: ConversationCreateRequest,
        service: MemoryServiceProtocol = Depends(service_dependency),  # noqa: FAST002
    ) -> ConversationEntry:
        try:
            item = await service.create_conversation_entry(
                group_id=payload.group_id,
                summary=payload.summary,
                participants=payload.participants,
                importance_initial=payload.importance_initial,
                importance_current=payload.importance_current,
                start_time=payload.start_time,
                end_time=payload.end_time,
                last_accessed=payload.last_accessed,
            )
        except ValueError as exc:
            raise _validation_error(str(exc)) from exc
        return ConversationEntry.model_validate(item)

    @router.patch("/conversations/{conversation_id}", response_model=ConversationEntry)
    async def update_conversation(
        conversation_id: int,
        payload: ConversationUpdateRequest,
        service: MemoryServiceProtocol = Depends(service_dependency),  # noqa: FAST002
    ) -> ConversationEntry:
        try:
            item = await service.update_conversation_entry(
                conversation_id,
                **_resolve_conversation_patch_params(payload),
            )
        except ValueError as exc:
            raise _validation_error(str(exc)) from exc
        if item is None:
            raise _conversation_not_found(conversation_id)
        return ConversationEntry.model_validate(item)

    @router.delete(
        "/conversations/{conversation_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    async def delete_conversation(
        conversation_id: int,
        service: MemoryServiceProtocol = Depends(service_dependency),  # noqa: FAST002
    ) -> Response:
        deleted = await service.delete_conversation_entry(conversation_id)
        if not deleted:
            raise _conversation_not_found(conversation_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get("/user-profiles", response_model=MemoryEntityListResponse)
    async def list_user_profiles(
        service: MemoryServiceProtocol = Depends(service_dependency),  # noqa: FAST002
        group_id: Annotated[str | None, Query(min_length=1)] = None,
        user_id: Annotated[str | None, Query(min_length=1)] = None,
        q: Annotated[str | None, Query(min_length=1)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> MemoryEntityListResponse:
        items, total = await service.list_user_profile_rows(
            limit=limit,
            offset=offset,
            group_id=group_id,
            user_id=user_id,
            query=q,
        )
        return MemoryEntityListResponse(
            items=[MemoryEntityEntry.model_validate(item) for item in items],
            total=total,
            limit=limit,
            offset=offset,
        )

    @router.get(
        "/user-profiles/{group_id}/{user_id}",
        response_model=MemoryEntityEntry,
    )
    async def get_user_profile(
        group_id: str,
        user_id: str,
        service: MemoryServiceProtocol = Depends(service_dependency),  # noqa: FAST002
    ) -> MemoryEntityEntry:
        item = await service.get_user_profile_row(user_id=user_id, group_id=group_id)
        if item is None:
            raise _user_profile_not_found(group_id, user_id)
        return MemoryEntityEntry.model_validate(item)

    @router.put(
        "/user-profiles/{group_id}/{user_id}",
        response_model=MemoryEntityEntry,
    )
    async def put_user_profile(
        group_id: str,
        user_id: str,
        payload: Annotated[dict[str, Any], Body(...)],
        importance: Annotated[int, Query(ge=1, le=5)] = 4,
        service: MemoryServiceProtocol = Depends(service_dependency),  # noqa: FAST002
    ) -> MemoryEntityEntry:
        _ensure_payload_user_id(payload, user_id)
        item = await service.upsert_user_profile_row(
            user_id=user_id,
            group_id=group_id,
            profile=payload,
            importance=importance,
        )
        if item is None:
            raise _service_unavailable()
        return MemoryEntityEntry.model_validate(item)

    @router.delete(
        "/user-profiles/{group_id}/{user_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    async def delete_user_profile(
        group_id: str,
        user_id: str,
        service: MemoryServiceProtocol = Depends(service_dependency),  # noqa: FAST002
    ) -> Response:
        deleted = await service.delete_user_profile(user_id=user_id, group_id=group_id)
        if not deleted:
            raise _user_profile_not_found(group_id, user_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get("/interaction-histories", response_model=MemoryEntityListResponse)
    async def list_interaction_histories(
        service: MemoryServiceProtocol = Depends(service_dependency),  # noqa: FAST002
        group_id: Annotated[str | None, Query(min_length=1)] = None,
        user_id: Annotated[str | None, Query(min_length=1)] = None,
        q: Annotated[str | None, Query(min_length=1)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> MemoryEntityListResponse:
        items, total = await service.list_interaction_history_rows(
            limit=limit,
            offset=offset,
            group_id=group_id,
            user_id=user_id,
            query=q,
        )
        return MemoryEntityListResponse(
            items=[MemoryEntityEntry.model_validate(item) for item in items],
            total=total,
            limit=limit,
            offset=offset,
        )

    @router.get(
        "/interaction-histories/{group_id}/{user_id}",
        response_model=MemoryEntityEntry,
    )
    async def get_interaction_history(
        group_id: str,
        user_id: str,
        service: MemoryServiceProtocol = Depends(service_dependency),  # noqa: FAST002
    ) -> MemoryEntityEntry:
        item = await service.get_interaction_history_row(
            user_id=user_id,
            group_id=group_id,
        )
        if item is None:
            raise _interaction_history_not_found(group_id, user_id)
        return MemoryEntityEntry.model_validate(item)

    @router.put(
        "/interaction-histories/{group_id}/{user_id}",
        response_model=MemoryEntityEntry,
    )
    async def put_interaction_history(
        group_id: str,
        user_id: str,
        payload: Annotated[dict[str, Any], Body(...)],
        importance: Annotated[int, Query(ge=1, le=5)] = 5,
        service: MemoryServiceProtocol = Depends(service_dependency),  # noqa: FAST002
    ) -> MemoryEntityEntry:
        _ensure_payload_user_id(payload, user_id)
        item = await service.upsert_interaction_history_row(
            user_id=user_id,
            group_id=group_id,
            interaction=payload,
            importance=importance,
        )
        if item is None:
            raise _service_unavailable()
        return MemoryEntityEntry.model_validate(item)

    @router.delete(
        "/interaction-histories/{group_id}/{user_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    async def delete_interaction_history(
        group_id: str,
        user_id: str,
        service: MemoryServiceProtocol = Depends(service_dependency),  # noqa: FAST002
    ) -> Response:
        deleted = await service.delete_interaction_history(
            user_id=user_id,
            group_id=group_id,
        )
        if not deleted:
            raise _interaction_history_not_found(group_id, user_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


def register_memory_api(
    app: FastAPI,
    *,
    api_token: str,
    allowed_origins: Sequence[str],
    service_getter: Callable[[], MemoryServiceProtocol | None],
) -> None:
    """注册记忆库 REST API 与共享 CORS。"""
    if getattr(app.state, "komari_memory_api_registered", False):
        return

    ensure_management_cors(app, allowed_origins)
    app.include_router(
        create_memory_router(
            api_token=api_token,
            service_getter=service_getter,
        )
    )
    app.state.komari_memory_api_registered = True
