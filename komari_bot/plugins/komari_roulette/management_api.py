"""TSK-279 Stage-C2 roulette management control plane.

Frozen REST surface (see §14 of the C2 contract):

* ``GET  /api/v2/komari-roulette/status``               -- ``roulette:read``
* ``POST /api/v2/komari-roulette/leaderboards/inspect`` -- ``roulette:read``
* ``POST /api/v2/komari-roulette/leaderboards/rebuild`` -- ``roulette:manage``
  plus the shared ``X-Komari-Change-Reason`` / ``X-Request-ID`` write headers.

The control plane is an operations/observability channel: it never reads the
roulette dynamic config, never checks the group-admission gate and exposes no
reset / forced-win / preview-token bypass.  Failures are projected onto the
fixed ``{"detail": {"code", "message"}}`` shell with a closed code set so raw
exception text can never leak through an HTTP response.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, field_validator

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

from .domain import GroupRef
from .storage import (
    AggregateCorruptError,
    PostgresRouletteStorage,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from komari_bot.management.management_api import ManagementTokenSource
    from komari_bot.management.management_audit import ManagementAuditRecorder

API_PREFIX = "/api/v2/komari-roulette"
ROULETTE_MANAGEMENT_ERROR_CODES: frozenset[str] = frozenset(
    {
        "roulette_status_unavailable",
        "roulette_storage_unavailable",
        "roulette_aggregate_corrupt",
    }
)

_REBUILD_ACTION = "roulette.leaderboard.rebuild"
_ROULETTE_RESOURCE = "roulette"
_STATUS_UNAVAILABLE_CODE = "roulette_status_unavailable"
_STORAGE_UNAVAILABLE_CODE = "roulette_storage_unavailable"
_AGGREGATE_CORRUPT_CODE = "roulette_aggregate_corrupt"
_STATUS_UNAVAILABLE_MESSAGE = "轮盘状态暂不可用"
_STORAGE_UNAVAILABLE_MESSAGE = "轮盘存储暂不可用"
_AGGREGATE_CORRUPT_MESSAGE = "轮盘聚合数据损坏"


class _LeaderboardScopeRequest(BaseModel):
    """Exact scope body; extra keys and blank identifiers are rejected."""

    model_config = ConfigDict(extra="forbid")

    app_id: str
    group_openid: str

    @field_validator("app_id", "group_openid")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        identifier = value.strip()
        if not identifier:
            msg = "标识符不能为空"
            raise ValueError(msg)
        return identifier


def _fixed_error(status_code: int, code: str, message: str) -> HTTPException:
    """Build the fixed ``{"detail": {"code", "message"}}`` error shell."""

    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )


def _status_error() -> HTTPException:
    return _fixed_error(
        503,
        _STATUS_UNAVAILABLE_CODE,
        _STATUS_UNAVAILABLE_MESSAGE,
    )


def _status_payload(observation: object | None) -> dict[str, Any]:
    """Project the frozen safe status snapshot, failing closed on a bad shape."""

    if observation is None:
        raise _status_error()
    as_dict = getattr(observation, "as_dict", None)
    if not callable(as_dict):
        raise _status_error()
    payload = as_dict()
    if not isinstance(payload, dict):
        raise _status_error()
    return payload


def _storage_error(error: BaseException) -> HTTPException:
    if isinstance(error, AggregateCorruptError):
        return _fixed_error(503, _AGGREGATE_CORRUPT_CODE, _AGGREGATE_CORRUPT_MESSAGE)
    return _fixed_error(
        503,
        _STORAGE_UNAVAILABLE_CODE,
        _STORAGE_UNAVAILABLE_MESSAGE,
    )


def _resolve_observation_getter(
    getter: Callable[[], object | None] | None,
) -> Callable[[], object | None]:
    if getter is not None:
        return getter

    def _default() -> object | None:
        from .lifecycle import get_roulette_observation

        return get_roulette_observation()

    return _default


def _resolve_session_factory(
    factory: Callable[[], Any] | None,
) -> Callable[[], Any]:
    if factory is not None:
        return factory

    def _default() -> Any:
        from nonebot_plugin_orm import get_session

        return get_session()

    return _default


def _resolve_storage_factory(
    factory: Callable[[Any], Any] | None,
) -> Callable[[Any], Any]:
    if factory is not None:
        return factory

    def _default(session: Any) -> Any:
        return PostgresRouletteStorage(session)

    return _default


def _inspection_payload(inspection: object) -> dict[str, Any]:
    as_dict = getattr(inspection, "as_dict", None)
    if not callable(as_dict):
        raise _fixed_error(503, _STORAGE_UNAVAILABLE_CODE, _STORAGE_UNAVAILABLE_MESSAGE)
    payload = as_dict()
    if not isinstance(payload, dict):
        raise _fixed_error(503, _STORAGE_UNAVAILABLE_CODE, _STORAGE_UNAVAILABLE_MESSAGE)
    return payload


def register_roulette_management_api(
    app: FastAPI,
    *,
    api_token: ManagementTokenSource,
    allowed_origins: Sequence[str],
    observation_getter: Callable[[], object | None] | None = None,
    session_factory: Callable[[], Any] | None = None,
    storage_factory: Callable[[Any], Any] | None = None,
    audit_recorder: ManagementAuditRecorder | None = None,
) -> None:
    """Mount the frozen three-route roulette control plane (idempotent)."""

    if getattr(app.state, "komari_roulette_management_api_registered", False):
        return

    ensure_management_cors(app, allowed_origins)
    read_auth_dependency = create_bearer_auth_dependency(
        api_token,
        detail="未授权访问轮盘接口",
        required_permission="roulette:read",
    )
    manage_auth_dependency = create_bearer_auth_dependency(
        api_token,
        detail="没有管理轮盘排行榜的权限",
        required_permission="roulette:manage",
    )
    recorder = audit_recorder or record_management_audit_event
    resolve_observation = _resolve_observation_getter(observation_getter)
    resolve_session = _resolve_session_factory(session_factory)
    resolve_storage = _resolve_storage_factory(storage_factory)

    router = APIRouter(prefix=API_PREFIX, tags=["komari-roulette"])

    @router.get(
        "/status",
        response_model=None,
        dependencies=[Depends(read_auth_dependency)],
    )
    async def roulette_status() -> dict[str, Any]:
        """Project the owner's frozen safe observation; never fabricate ready."""

        try:
            observation = resolve_observation()
            payload = _status_payload(observation)
        except HTTPException:
            raise
        except Exception:
            raise _status_error() from None
        return payload

    @router.post(
        "/leaderboards/inspect",
        response_model=None,
        dependencies=[Depends(read_auth_dependency)],
    )
    async def inspect_leaderboard(
        payload: Annotated[_LeaderboardScopeRequest, Body()],
    ) -> dict[str, Any]:
        """Reconcile the cached projection against completed proofs (read-only)."""

        group = GroupRef(payload.app_id, payload.group_openid)
        try:
            async with resolve_session() as session:
                storage = resolve_storage(session)
                inspection = await storage.inspect_leaderboard(group)
            return _inspection_payload(inspection)
        except HTTPException:
            raise
        except Exception as error:
            raise _storage_error(error) from None

    @router.post(
        "/leaderboards/rebuild",
        response_model=None,
        dependencies=[Depends(manage_auth_dependency)],
    )
    async def rebuild_leaderboard(
        payload: Annotated[_LeaderboardScopeRequest, Body()],
        principal: ManagementPrincipal = Depends(  # noqa: FAST002
            manage_auth_dependency
        ),
        change_reason: str = Depends(  # noqa: FAST002
            require_management_change_reason
        ),
        request_id: str = Depends(require_management_request_id),  # noqa: FAST002
    ) -> dict[str, Any]:
        """Rebuild the projection from completed proofs behind one audit span."""

        group = GroupRef(payload.app_id, payload.group_openid)
        target_hash = hash_management_target(payload.app_id, payload.group_openid)
        try:
            async with management_audit_span(
                principal=principal,
                request_id=request_id,
                reason=change_reason,
                action=_REBUILD_ACTION,
                resource=_ROULETTE_RESOURCE,
                target_hash=target_hash,
                recorder=recorder,
            ) as audit:
                try:
                    async with resolve_session() as session:
                        storage = resolve_storage(session)
                        await storage.rebuild_leaderboard(group)
                        inspection = await storage.inspect_leaderboard(group)
                        await session.commit()
                except HTTPException:
                    raise
                except Exception as error:
                    raise _storage_error(error) from None
                audit.metadata.update(
                    {
                        "entry_count": inspection.cached_entry_count,
                        "total_wins": inspection.cached_total_wins,
                        "result_code": "rebuilt",
                    }
                )
                return {
                    "app_id": payload.app_id,
                    "group_openid": payload.group_openid,
                    "consistent": bool(inspection.consistent),
                    "entry_count": inspection.cached_entry_count,
                    "total_wins": inspection.cached_total_wins,
                }
        except HTTPException:
            raise
        except Exception as error:
            raise _storage_error(error) from None

    for route in router.routes:
        app.router.routes.append(route)
    app.openapi_schema = None
    app.state.komari_roulette_management_api_registered = True


__all__ = [
    "API_PREFIX",
    "ROULETTE_MANAGEMENT_ERROR_CODES",
    "register_roulette_management_api",
]
