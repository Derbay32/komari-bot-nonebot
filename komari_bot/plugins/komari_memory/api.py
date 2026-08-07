"""Komari Memory REST API。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Protocol

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Response
from starlette import status

from komari_bot.llm.content_budget import (
    IDENTIFIER_TEXT_BUDGET,
    QUERY_TEXT_BUDGET,
    ContentValidationError,
    normalize_required_text,
)
from komari_bot.management.management_api import (
    create_bearer_auth_dependency,
    ensure_management_cors,
)

from .api_models import (
    ConversationCreateRequest,
    ConversationDeadLetterEntry,
    ConversationDeadLetterListResponse,
    ConversationDeadLetterRequeueResponse,
    ConversationEntry,
    ConversationListResponse,
    ConversationUpdateRequest,
    InteractionEventEntry,
    InteractionEventListResponse,
    InteractionEventUpdateRequest,
    MemoryEntityEntry,
    MemoryEntityListResponse,
    UserProfileUpsertRequest,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from komari_bot.management.management_api import ManagementTokenSource

    from .services.conversation_processing import ConversationDeadLetter

API_PREFIX = "/api/v2/komari-memory"


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
        importance_current: int | None = None,
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
        importance_current: int | None = None,
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

    async def upsert_user_profile_row(
        self,
        *,
        user_id: str,
        group_id: str,
        profile: dict[str, Any],
        importance: int = 4,
    ) -> dict[str, Any] | None: ...

    async def delete_user_profile(
        self,
        *,
        user_id: str,
        group_id: str,
    ) -> bool: ...

    async def get_interaction_event_entry(
        self,
        event_id: int,
    ) -> dict[str, Any] | None: ...

    async def update_interaction_event_entry(
        self,
        event_id: int,
        *,
        event_summary: str | None = None,
        importance_initial: int | None = None,
        importance_current: int | None = None,
    ) -> dict[str, Any] | None: ...

    async def delete_interaction_event_entry(self, event_id: int) -> bool: ...


class ConversationDeadLetterManagerProtocol(Protocol):
    """REST API 需要的最小 Redis dead-letter 协议。"""

    async def list_conversation_dead_letters(
        self,
        *,
        limit: int = 100,
    ) -> list[ConversationDeadLetter]: ...

    async def requeue_conversation_dead_letter(
        self,
        *,
        group_id: str,
        snapshot_id: str,
    ) -> int | None: ...


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


def _dead_letter_service_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Komari Memory Redis dead-letter 服务未初始化",
    )


def _user_id_mismatch_error() -> HTTPException:
    return _validation_error("请求体中的 user_id 与路径参数不一致")


def _conversation_not_found(conversation_id: int) -> HTTPException:
    return _not_found(f"未找到 ID={conversation_id} 的对话记忆")


def _user_profile_not_found(group_id: str, user_id: str) -> HTTPException:
    return _not_found(f"未找到 group={group_id} user={user_id} 的用户画像")


def _interaction_event_not_found(event_id: int) -> HTTPException:
    return _not_found(f"未找到 ID={event_id} 的互动事件记忆")


def _build_service_dependency(
    service_getter: Callable[[], MemoryServiceProtocol | None],
) -> Callable[[], MemoryServiceProtocol]:
    def _get_service() -> MemoryServiceProtocol:
        service = service_getter()
        if service is None:
            raise _service_unavailable()
        return service

    return _get_service


def _build_dead_letter_dependency(
    redis_getter: Callable[[], ConversationDeadLetterManagerProtocol | None] | None,
) -> Callable[[], ConversationDeadLetterManagerProtocol]:
    def _get_redis_manager() -> ConversationDeadLetterManagerProtocol:
        redis_manager = redis_getter() if redis_getter is not None else None
        if redis_manager is None:
            raise _dead_letter_service_unavailable()
        return redis_manager

    return _get_redis_manager


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


def _normalize_identifier(value: str, *, label: str) -> str:
    try:
        return normalize_required_text(
            value,
            label=label,
            budget=IDENTIFIER_TEXT_BUDGET,
        )
    except ContentValidationError as exc:
        raise _validation_error(str(exc)) from exc


def _normalize_redis_key_component(value: str, *, label: str) -> str:
    normalized = _normalize_identifier(value, label=label)
    if ":" in normalized:
        message = f"{label} 不能包含冒号"
        raise _validation_error(message)
    return normalized


def _normalize_optional_identifier(value: str | None, *, label: str) -> str | None:
    if value is None:
        return None
    return _normalize_identifier(value, label=label)


def _normalize_optional_query(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return normalize_required_text(
            value,
            label="查询文本",
            budget=QUERY_TEXT_BUDGET,
        )
    except ContentValidationError as exc:
        raise _validation_error(str(exc)) from exc


def _resolve_interaction_event_patch_params(
    payload: InteractionEventUpdateRequest,
) -> dict[str, Any]:
    fields_set = payload.model_fields_set
    if not fields_set:
        raise _validation_error("至少提供一个要更新的字段")
    return {
        "event_summary": payload.event_summary if "event_summary" in fields_set else None,
        "importance_initial": (
            payload.importance_initial if "importance_initial" in fields_set else None
        ),
        "importance_current": (
            payload.importance_current if "importance_current" in fields_set else None
        ),
    }


def create_memory_router(
    *,
    api_token: ManagementTokenSource,
    service_getter: Callable[[], MemoryServiceProtocol | None],
    redis_getter: Callable[[], ConversationDeadLetterManagerProtocol | None] | None = None,
) -> APIRouter:
    """创建记忆库管理路由。"""
    read_auth_dependency = create_bearer_auth_dependency(
        api_token,
        detail="未授权访问 Komari Memory 管理接口",
        required_permission="memory:read",
    )
    write_auth_dependency = create_bearer_auth_dependency(
        api_token,
        detail="没有修改 Komari Memory 的权限",
        required_permission="memory:write",
    )
    service_dependency = _build_service_dependency(service_getter)
    dead_letter_dependency = _build_dead_letter_dependency(redis_getter)
    router = APIRouter(
        prefix=API_PREFIX,
        dependencies=[Depends(read_auth_dependency)],
        tags=["komari-memory"],
    )

    @router.get(
        "/conversation-dead-letters",
        response_model=ConversationDeadLetterListResponse,
    )
    async def list_conversation_dead_letters(
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        redis_manager: ConversationDeadLetterManagerProtocol = Depends(  # noqa: FAST002
            dead_letter_dependency
        ),
    ) -> ConversationDeadLetterListResponse:
        dead_letters = await redis_manager.list_conversation_dead_letters(limit=limit)
        return ConversationDeadLetterListResponse(
            items=[
                ConversationDeadLetterEntry(
                    group_id=item.group_id,
                    snapshot_id=item.snapshot_id,
                    failure_code=item.failure_code,
                    attempt_count=item.attempt_count,
                    failed_at_ms=item.failed_at_ms,
                    message_count=item.message_count,
                    chunk_state_count=item.chunk_state_count,
                )
                for item in dead_letters
            ],
            limit=limit,
        )

    @router.post(
        "/conversation-dead-letters/{group_id}/{snapshot_id}/requeue",
        response_model=ConversationDeadLetterRequeueResponse,
        dependencies=[Depends(write_auth_dependency)],
    )
    async def requeue_conversation_dead_letter(
        group_id: str,
        snapshot_id: str,
        redis_manager: ConversationDeadLetterManagerProtocol = Depends(  # noqa: FAST002
            dead_letter_dependency
        ),
    ) -> ConversationDeadLetterRequeueResponse:
        normalized_group_id = _normalize_redis_key_component(
            group_id,
            label="群组 ID",
        )
        normalized_snapshot_id = _normalize_redis_key_component(
            snapshot_id,
            label="快照 ID",
        )
        restored_count = await redis_manager.requeue_conversation_dead_letter(
            group_id=normalized_group_id,
            snapshot_id=normalized_snapshot_id,
        )
        if restored_count is None:
            raise _not_found("未找到指定的对话总结失败快照")
        return ConversationDeadLetterRequeueResponse(
            group_id=normalized_group_id,
            snapshot_id=normalized_snapshot_id,
            restored_message_count=restored_count,
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
        normalized_group_id = _normalize_optional_identifier(
            group_id,
            label="群组 ID",
        )
        normalized_participant = _normalize_optional_identifier(
            participant,
            label="参与者 ID",
        )
        items, total = await service.list_conversations(
            limit=limit,
            offset=offset,
            group_id=normalized_group_id,
            participant=normalized_participant,
            query=_normalize_optional_query(q),
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
        dependencies=[Depends(write_auth_dependency)],
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

    @router.patch(
        "/conversations/{conversation_id}",
        response_model=ConversationEntry,
        dependencies=[Depends(write_auth_dependency)],
    )
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
        dependencies=[Depends(write_auth_dependency)],
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
            group_id=_normalize_optional_identifier(group_id, label="群组 ID"),
            user_id=_normalize_optional_identifier(user_id, label="用户 ID"),
            query=_normalize_optional_query(q),
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
        group_id = _normalize_identifier(group_id, label="群组 ID")
        user_id = _normalize_identifier(user_id, label="用户 ID")
        item = await service.get_user_profile_row(user_id=user_id, group_id=group_id)
        if item is None:
            raise _user_profile_not_found(group_id, user_id)
        return MemoryEntityEntry.model_validate(item)

    @router.put(
        "/user-profiles/{group_id}/{user_id}",
        response_model=MemoryEntityEntry,
        dependencies=[Depends(write_auth_dependency)],
    )
    async def put_user_profile(
        group_id: str,
        user_id: str,
        payload: UserProfileUpsertRequest,
        importance: Annotated[int, Query(ge=1, le=5)] = 4,
        service: MemoryServiceProtocol = Depends(service_dependency),  # noqa: FAST002
    ) -> MemoryEntityEntry:
        group_id = _normalize_identifier(group_id, label="群组 ID")
        user_id = _normalize_identifier(user_id, label="用户 ID")
        profile = payload.root
        _ensure_payload_user_id(profile, user_id)
        item = await service.upsert_user_profile_row(
            user_id=user_id,
            group_id=group_id,
            profile=profile,
            importance=importance,
        )
        if item is None:
            raise _service_unavailable()
        return MemoryEntityEntry.model_validate(item)

    @router.delete(
        "/user-profiles/{group_id}/{user_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        dependencies=[Depends(write_auth_dependency)],
    )
    async def delete_user_profile(
        group_id: str,
        user_id: str,
        service: MemoryServiceProtocol = Depends(service_dependency),  # noqa: FAST002
    ) -> Response:
        group_id = _normalize_identifier(group_id, label="群组 ID")
        user_id = _normalize_identifier(user_id, label="用户 ID")
        deleted = await service.delete_user_profile(user_id=user_id, group_id=group_id)
        if not deleted:
            raise _user_profile_not_found(group_id, user_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get("/interactions", response_model=InteractionEventListResponse)
    async def list_interactions(
        service: MemoryServiceProtocol = Depends(service_dependency),  # noqa: FAST002
        user_id: Annotated[str | None, Query(min_length=1)] = None,
        q: Annotated[str | None, Query(min_length=1)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> InteractionEventListResponse:
        items, total = await service.list_interaction_history_rows(
            limit=limit,
            offset=offset,
            user_id=_normalize_optional_identifier(user_id, label="用户 ID"),
            query=_normalize_optional_query(q),
        )
        return InteractionEventListResponse(
            items=[InteractionEventEntry.model_validate(item) for item in items],
            total=total,
            limit=limit,
            offset=offset,
        )

    @router.get("/interactions/{event_id}", response_model=InteractionEventEntry)
    async def get_interaction_event(
        event_id: int,
        service: MemoryServiceProtocol = Depends(service_dependency),  # noqa: FAST002
    ) -> InteractionEventEntry:
        item = await service.get_interaction_event_entry(event_id)
        if item is None:
            raise _interaction_event_not_found(event_id)
        return InteractionEventEntry.model_validate(item)

    @router.patch(
        "/interactions/{event_id}",
        response_model=InteractionEventEntry,
        dependencies=[Depends(write_auth_dependency)],
    )
    async def update_interaction_event(
        event_id: int,
        payload: InteractionEventUpdateRequest,
        service: MemoryServiceProtocol = Depends(service_dependency),  # noqa: FAST002
    ) -> InteractionEventEntry:
        item = await service.update_interaction_event_entry(
            event_id,
            **_resolve_interaction_event_patch_params(payload),
        )
        if item is None:
            raise _interaction_event_not_found(event_id)
        return InteractionEventEntry.model_validate(item)

    @router.delete(
        "/interactions/{event_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        dependencies=[Depends(write_auth_dependency)],
    )
    async def delete_interaction_event(
        event_id: int,
        service: MemoryServiceProtocol = Depends(service_dependency),  # noqa: FAST002
    ) -> Response:
        deleted = await service.delete_interaction_event_entry(event_id)
        if not deleted:
            raise _interaction_event_not_found(event_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


def register_memory_api(
    app: FastAPI,
    *,
    api_token: ManagementTokenSource,
    allowed_origins: Sequence[str],
    service_getter: Callable[[], MemoryServiceProtocol | None],
    redis_getter: Callable[[], ConversationDeadLetterManagerProtocol | None] | None = None,
) -> None:
    """注册记忆库 REST API 与共享 CORS。"""
    if getattr(app.state, "komari_memory_api_registered", False):
        return

    ensure_management_cors(app, allowed_origins)
    app.include_router(
        create_memory_router(
            api_token=api_token,
            service_getter=service_getter,
            redis_getter=redis_getter,
        )
    )
    app.state.komari_memory_api_registered = True
