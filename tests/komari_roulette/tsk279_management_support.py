"""TSK-279 Stage-C2 管理控制面测试支撑（测试专用，不承载生产语义）。

C2 冻结的接缝（生产实现必须满足，否则用例红）：

1. ``komari_bot.plugins.komari_roulette.management_api`` 暴露
   ``API_PREFIX == "/api/v2/komari-roulette"`` 与唯一装配入口
   ``register_roulette_management_api(app, *, api_token, allowed_origins,
   observation_getter=None, session_factory=None, storage_factory=None,
   audit_recorder=None)``；注册幂等，路由面精确等于三条
   （``GET /status``、``POST /leaderboards/inspect``、
   ``POST /leaderboards/rebuild``）。
2. ``komari_bot.plugins.komari_roulette`` 顶层 ``__all__`` 暴露
   ``get_roulette_observation`` / ``register_roulette_management_api`` /
   ``LeaderboardInspection``（跨插件只走顶层公开面，ADR-0006）。
3. 只读检查面是 ``PostgresRouletteStorage.inspect_leaderboard(group) ->
   LeaderboardInspection``（一致快照；缺行 / 多行 / 错 wins / 成员错配 /
   显示名错配 / 时间错配），损坏的 completed 证明必须抛
   ``AggregateCorruptError``，绝不静默伪 0。
4. 错误体固定外壳 ``{"detail": {"code", "message"}}``，``code`` 属
   ``ROULETTE_ERROR_CODES`` 闭集，``message`` 固定文案（绝不回显异常正文）。

本 Module 只做测试装配：真实 FastAPI + 真实 ``register_*`` 入口；存储与会话用
可控 fake，真实 PostgreSQL 验收见
``test_tsk279_leaderboard_management_pg.py``。
"""

from __future__ import annotations

import importlib
import inspect
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import FastAPI
from fastapi.routing import APIRoute
from pydantic import BaseModel

from komari_bot.plugins.komari_roulette.observability import RouletteObservation

from .tsk279_support import load_module, load_symbol

if TYPE_CHECKING:
    from collections.abc import (
        AsyncIterator,
        Callable,
        Iterable,
        Iterator,
        Mapping,
        Sequence,
    )

    from komari_bot.management.management_audit import ManagementAuditEvent

ROULETTE_PACKAGE = "komari_bot.plugins.komari_roulette"
ROULETTE_MANAGEMENT_MODULE = f"{ROULETTE_PACKAGE}.management_api"
ROULETTE_STORAGE_MODULE = f"{ROULETTE_PACKAGE}.storage"
ROULETTE_LIFECYCLE_MODULE = f"{ROULETTE_PACKAGE}.lifecycle"
MANAGEMENT_MODULE = "komari_bot.plugins.komari_management"
MANAGEMENT_RUNTIME_MODULE = f"{MANAGEMENT_MODULE}.api_runtime"

#: Frozen REST surface (see §14 of the C2 contract).
API_PREFIX = "/api/v2/komari-roulette"
STATUS_PATH = f"{API_PREFIX}/status"
INSPECT_PATH = f"{API_PREFIX}/leaderboards/inspect"
REBUILD_PATH = f"{API_PREFIX}/leaderboards/rebuild"
EXPECTED_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        (STATUS_PATH, "GET"),
        (INSPECT_PATH, "POST"),
        (REBUILD_PATH, "POST"),
    }
)
#: 控制面不得出现的入口词（reset / 任意改 win / 重发 / 预览令牌）。
FORBIDDEN_PATH_WORDS: tuple[str, ...] = (
    "reset",
    "force",
    "bypass",
    "grant",
    "wins",
    "preview",
    "confirm",
    "token",
    "dry",
    "resend",
)

#: 固定错误外壳的封闭 code 集（冻结）。
ROULETTE_ERROR_CODES: frozenset[str] = frozenset(
    {
        "roulette_status_unavailable",
        "roulette_storage_unavailable",
        "roulette_aggregate_corrupt",
    }
)

#: 排行榜校验差异 code 闭集（冻结；生产不必导出同名常量）。
DISCREPANCY_CODES: frozenset[str] = frozenset(
    {
        "missing_cached_row",
        "extra_cached_row",
        "wins_mismatch",
        "display_name_mismatch",
        "last_won_at_mismatch",
    }
)

STATUS_RESPONSE_FIELDS: frozenset[str] = RouletteObservation.FIELDS
INSPECT_RESPONSE_FIELDS: frozenset[str] = frozenset(
    {
        "app_id",
        "group_openid",
        "consistent",
        "cached_entry_count",
        "completed_entry_count",
        "cached_total_wins",
        "completed_total_wins",
        "discrepancy_codes",
        "entries",
    }
)
REBUILD_RESPONSE_FIELDS: frozenset[str] = frozenset(
    {
        "app_id",
        "group_openid",
        "consistent",
        "entry_count",
        "total_wins",
    }
)
ENTRY_RESPONSE_FIELDS: frozenset[str] = frozenset(
    {"display_name", "wins", "last_won_at"}
)

READER_TOKEN = "roulette-reader-token-00000"
MANAGER_TOKEN = "roulette-manager-token-0000"
WILDCARD_TOKEN = "roulette-wildcard-token-000"
FOREIGN_TOKEN = "roulette-foreign-token-0000"

REBUILD_ACTION = "roulette.leaderboard.rebuild"
REBUILD_RESOURCE = "roulette"
REBUILD_RESULT_CODE = "rebuilt"

T0 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)

#: Sentinel: omit ``observation_getter`` so the production default is used.
UNSET: object = object()

#: ``(status, reason)`` pairs the control plane must surface without rejecting.
STATUS_LIFECYCLE_CASES: tuple[tuple[str, str], ...] = (
    ("ready", "policy_admitted"),
    ("disabled", "plugin_disabled"),
    ("failed", "recovery_failed"),
)


def install_real_observation_owner(
    monkeypatch: Any,
    *,
    status: str,
    reason: str,
) -> Any:
    """Point the real ``get_roulette_observation`` getter at a structural owner.

    Only the process-local owner slot is replaced.  The getter, the runtime-state
    normalization and the observability snapshot projection remain the real
    production code paths, so a route that returns this observation through the
    default getter proves the status surface, not a hand-built projection.
    """

    from types import SimpleNamespace

    from komari_bot.plugins.komari_roulette import lifecycle
    from komari_bot.plugins.komari_roulette.observability import RouletteObservability

    runtime_state = SimpleNamespace(status=status, reason_code=reason)
    owner = SimpleNamespace(
        runtime=SimpleNamespace(get_state=lambda: runtime_state),
        observability=RouletteObservability(),
    )
    monkeypatch.setattr(lifecycle._state, "app", owner)
    return owner


__all__ = [
    "API_PREFIX",
    "DISCREPANCY_CODES",
    "ENTRY_RESPONSE_FIELDS",
    "EXPECTED_ROUTES",
    "FORBIDDEN_PATH_WORDS",
    "FOREIGN_TOKEN",
    "INSPECT_PATH",
    "INSPECT_RESPONSE_FIELDS",
    "MANAGER_TOKEN",
    "READER_TOKEN",
    "REBUILD_ACTION",
    "REBUILD_PATH",
    "REBUILD_RESOURCE",
    "REBUILD_RESPONSE_FIELDS",
    "ROULETTE_ERROR_CODES",
    "STATUS_LIFECYCLE_CASES",
    "STATUS_PATH",
    "STATUS_RESPONSE_FIELDS",
    "UNSET",
    "WILDCARD_TOKEN",
    "ControlPlane",
    "FakeLeaderboardStorage",
    "FakeSessionFactory",
    "RecordingAuditRecorder",
    "asgi_client",
    "assert_error_envelope",
    "auth_headers",
    "build_control_plane",
    "build_management_components",
    "inspect_body",
    "install_real_observation_owner",
    "leaderboard_entry",
    "load_management_module",
    "make_inspection",
    "management_credentials",
    "rebuild_headers",
    "registered_api_routes",
    "required_attr",
]


def load_management_module() -> Any:
    """Load the roulette management route module (missing module is the RED)."""

    return load_module(ROULETTE_MANAGEMENT_MODULE)


def required_attr(module: object, name: str) -> Any:
    """Read one required production symbol with a TSK-279 C2 RED message."""

    try:
        return getattr(module, name)
    except AttributeError as error:
        module_name = getattr(module, "__name__", str(module))
        message = f"{module_name}.{name} 尚未实现（TSK-279 C2 RED）"
        raise AttributeError(message) from error


def management_credentials() -> list[dict[str, object]]:
    """四凭据矩阵：只读 / 只管理 / 通配 / 无关权限。"""

    return [
        {
            "credential_id": "roulette-reader",
            "token": READER_TOKEN,
            "permissions": ["roulette:read"],
        },
        {
            "credential_id": "roulette-manager",
            "token": MANAGER_TOKEN,
            "permissions": ["roulette:manage"],
        },
        {
            "credential_id": "roulette-admin",
            "token": WILDCARD_TOKEN,
            "permissions": ["*"],
        },
        {
            "credential_id": "roulette-foreign",
            "token": FOREIGN_TOKEN,
            "permissions": ["config:read", "group_admission:read"],
        },
    ]


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def inspect_body(app_id: str, group_openid: str) -> dict[str, str]:
    return {"app_id": app_id, "group_openid": group_openid}


def rebuild_headers(
    token: str,
    *,
    reason: str = "tsk279-c2-verify-leaderboard",
    request_id: str = "tsk279-rebuild-0001",
) -> dict[str, str]:
    headers = auth_headers(token)
    headers["X-Komari-Change-Reason"] = reason
    headers["X-Request-ID"] = request_id
    return headers


def leaderboard_entry(
    *,
    display_name: str,
    wins: int,
    last_won_at: datetime,
) -> Any:
    """Build one safe ``LeaderboardEntry`` through the real top-level type."""

    entry_type = load_symbol(ROULETTE_PACKAGE, "LeaderboardEntry")
    return entry_type(
        display_name=display_name,
        wins=wins,
        last_won_at=last_won_at,
    )


def make_inspection(
    *,
    app_id: str,
    group_openid: str,
    cached: Sequence[Any] = (),
    completed: Sequence[Any] = (),
    discrepancy_codes: Sequence[str] = (),
    consistent: bool | None = None,
) -> Any:
    """Build a real ``LeaderboardInspection`` through the new storage seam type.

    The concrete home module is not pinned: the type only has to be re-exported
    from the roulette top-level surface (ADR-0006).
    """

    inspection_type = load_symbol(ROULETTE_PACKAGE, "LeaderboardInspection")
    resolved = not discrepancy_codes if consistent is None else consistent
    return inspection_type(
        app_id=app_id,
        group_openid=group_openid,
        consistent=resolved,
        cached_entry_count=len(cached),
        completed_entry_count=len(completed),
        cached_total_wins=sum(entry.wins for entry in cached),
        completed_total_wins=sum(entry.wins for entry in completed),
        discrepancy_codes=tuple(discrepancy_codes),
        entries=tuple(cached),
    )


# ---------------------------------------------------------------------------
# Fake storage / session / audit seams
# ---------------------------------------------------------------------------


def _group_key(group: object) -> tuple[object, object]:
    return (
        getattr(group, "app_id", None),
        getattr(group, "group_openid", None),
    )


@dataclass(slots=True)
class FakeLeaderboardStorage:
    """Controllable read/write seam: records calls and raises scripted errors."""

    inspection: Any = None
    inspect_error: BaseException | None = None
    rebuild_error: BaseException | None = None
    rebuilds: list[tuple[object, object]] = field(default_factory=list)
    inspect_keys: list[tuple[object, object]] = field(default_factory=list)
    inspects: int = 0

    async def inspect_leaderboard(self, group: object) -> Any:
        self.inspects += 1
        self.inspect_keys.append(_group_key(group))
        if self.inspect_error is not None:
            raise self.inspect_error
        if self.inspection is None:
            message = "fake inspection is not configured (TSK-279 C2 fixture)"
            raise AssertionError(message)
        return self.inspection

    async def rebuild_leaderboard(self, group: object) -> None:
        self.rebuilds.append(_group_key(group))
        if self.rebuild_error is not None:
            raise self.rebuild_error

    async def list_leaderboard(self, group: object) -> tuple[Any, ...]:
        del group
        if self.inspection is None:
            return ()
        return tuple(self.inspection.entries)


class _FakeSession:
    """Observe commit *attempts* vs *successes* plus a scripted commit failure.

    ``commit_attempts`` counts every ``commit()`` call; ``commits`` counts only
    the calls that returned.  A scripted ``commit_error`` therefore models a
    **known** failed commit (raised before any state is applied) instead of an
    ambiguous network outcome; the route must never report a partial success.
    """

    __slots__ = (
        "_commit_error",
        "closed",
        "commit_attempts",
        "commits",
        "rollbacks",
    )

    def __init__(self, *, commit_error: BaseException | None = None) -> None:
        self._commit_error = commit_error
        self.closed = False
        self.commit_attempts = 0
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commit_attempts += 1
        if self._commit_error is not None:
            raise self._commit_error
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def close(self) -> None:
        self.closed = True


class _FakeSessionContext:
    __slots__ = ("_session",)

    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeSession:
        return self._session

    async def __aexit__(self, *_exc: object) -> None:
        self._session.closed = True


class FakeSessionFactory:
    """``get_session``-shaped factory: callable -> async context manager.

    ``commit_error`` is scripted onto every session it hands out so a rebuild
    can exercise a known ``commit()`` failure without inventing partial state.
    """

    __slots__ = ("_commit_error", "sessions")

    def __init__(self, *, commit_error: BaseException | None = None) -> None:
        self._commit_error = commit_error
        self.sessions: list[_FakeSession] = []

    def __call__(self) -> _FakeSessionContext:
        session = _FakeSession(commit_error=self._commit_error)
        self.sessions.append(session)
        return _FakeSessionContext(session)

    @property
    def commits(self) -> int:
        return sum(session.commits for session in self.sessions)

    @property
    def commit_attempts(self) -> int:
        return sum(session.commit_attempts for session in self.sessions)

    @property
    def rollbacks(self) -> int:
        return sum(session.rollbacks for session in self.sessions)


class RecordingAuditRecorder:
    """Injected recorder: collects events, optionally probes, optionally fails."""

    __slots__ = ("_fail_on_outcome", "_probe", "events", "probes")

    def __init__(
        self,
        *,
        fail_on_outcome: str | None = None,
        probe: Callable[[], Any] | None = None,
    ) -> None:
        self.events: list[ManagementAuditEvent] = []
        self.probes: list[Any] = []
        self._fail_on_outcome = fail_on_outcome
        self._probe = probe

    async def __call__(self, event: ManagementAuditEvent) -> None:
        self.events.append(event)
        if self._probe is not None:
            self.probes.append(self._probe())
        if self._fail_on_outcome is not None and event.outcome == self._fail_on_outcome:
            message = "audit recorder is unavailable (TSK-279 C2 fixture)"
            raise RuntimeError(message)

    def final_events(self) -> list[ManagementAuditEvent]:
        return [event for event in self.events if event.outcome != "started"]

    def actions(self) -> list[str]:
        return [event.action for event in self.events]


# ---------------------------------------------------------------------------
# Route surface helpers (FastAPI 0.139 wraps included routers)
# ---------------------------------------------------------------------------


def _iter_api_routes(routes: Iterable[Any]) -> Iterator[APIRoute]:
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
            continue
        nested = getattr(route, "routes", None)
        if nested is None:
            original_router = getattr(route, "original_router", None)
            if original_router is not None:
                nested = getattr(original_router, "routes", None)
        if nested is not None:
            yield from _iter_api_routes(nested)


def registered_api_routes(app: FastAPI) -> set[tuple[str, str]]:
    return {
        (route.path, method)
        for route in _iter_api_routes(list(app.routes))
        for method in (route.methods or set())
    }


@asynccontextmanager
async def asgi_client(
    app: FastAPI,
    *,
    base_url: str = "http://roulette.test",
    raise_app_exceptions: bool = True,
) -> AsyncIterator[httpx.AsyncClient]:
    """Real ASGI client; opt out of re-raising to observe FastAPI's default 500."""

    transport = httpx.ASGITransport(
        app=app,
        raise_app_exceptions=raise_app_exceptions,
    )
    async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
        yield client


def assert_error_envelope(
    response: httpx.Response,
    status_code: int,
    code: str,
    *,
    leaked: Sequence[str] = (),
) -> dict[str, Any]:
    """Assert the exact fixed error shell; never echo the exception body."""

    assert code in ROULETTE_ERROR_CODES, f"测试自身使用了非封闭 code: {code}"
    assert response.status_code == status_code, response.text
    body = response.json()
    assert set(body) == {"detail"}, f"错误响应外壳不精确: {body}"
    detail = body["detail"]
    assert set(detail) == {"code", "message"}, f"错误 detail 键集不精确: {detail}"
    assert detail["code"] == code, detail
    message = detail["message"]
    assert isinstance(message, str)
    assert message.strip(), detail
    rendered = json.dumps(body, ensure_ascii=False, sort_keys=True)
    for needle in leaked:
        assert needle not in rendered, f"错误体泄露了 {needle!r}: {rendered}"
    return detail


# ---------------------------------------------------------------------------
# Control plane assembly
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ControlPlane:
    app: FastAPI
    module: Any
    storage: FakeLeaderboardStorage
    session_factory: Any
    audit: RecordingAuditRecorder
    registration_kwargs: dict[str, Any]


def build_control_plane(
    *,
    observation: object = UNSET,
    storage: FakeLeaderboardStorage | None = None,
    session_factory: Callable[[], Any] | None = None,
    audit: RecordingAuditRecorder | None = None,
    allowed_origins: Sequence[str] = (),
    credentials: Sequence[Mapping[str, object]] | None = None,
    real_storage: bool = False,
) -> ControlPlane:
    """Register the real route module on a fresh FastAPI app with fake storage.

    ``observation`` defaults to :data:`UNSET`, which omits ``observation_getter``
    so the production default (the real lifecycle getter) is exercised.
    ``real_storage=True`` omits ``storage_factory`` so the production
    ``PostgresRouletteStorage`` default is used (real-PG end to end).
    """

    module = load_management_module()
    resolved_storage = storage or FakeLeaderboardStorage()
    resolved_sessions: Callable[[], Any] = (
        FakeSessionFactory() if session_factory is None else session_factory
    )
    recorder = audit or RecordingAuditRecorder()
    app = FastAPI()
    registration_kwargs: dict[str, Any] = {
        "api_token": list(credentials or management_credentials()),
        "allowed_origins": list(allowed_origins),
        "session_factory": resolved_sessions,
        "audit_recorder": recorder,
    }
    if not real_storage:
        registration_kwargs["storage_factory"] = lambda _session: resolved_storage
    if observation is not UNSET:
        registration_kwargs["observation_getter"] = lambda: observation
    module.register_roulette_management_api(app, **registration_kwargs)
    return ControlPlane(
        app=app,
        module=module,
        storage=resolved_storage,
        session_factory=resolved_sessions,
        audit=recorder,
        registration_kwargs=registration_kwargs,
    )


class _FakeConfigSchema(BaseModel):
    plugin_enable: bool = True


class _FakeConfigManager:
    """Minimal ``ConfigManagerProtocol`` stand-in for route construction."""

    __slots__ = ()

    @property
    def config_source(self) -> str:
        return "postgres:komari_plugin_configs/komari_management"

    async def get_async(self) -> BaseModel:
        return _FakeConfigSchema()

    async def update_field_async(self, field_name: str, value: Any) -> BaseModel:
        del field_name, value
        return _FakeConfigSchema()

    async def reload_async(self) -> BaseModel:
        return _FakeConfigSchema()


ROULETTE_COMPONENT_FIELDS: tuple[str, ...] = (
    "register_roulette_management_api",
    "roulette_observation_getter",
)


def build_management_components() -> Any:
    """Build real ``ManagementApiComponents`` including the C2 roulette fields.

    A missing field is the expected C2 RED (the production assembly must not
    keep old component constructions green with a no-op default).
    """

    from komari_bot.plugins.agent_run_logger.api import register_agent_run_log_api
    from komari_bot.plugins.character_binding.management_api import (
        register_character_binding_repair_api,
    )
    from komari_bot.plugins.group_admission import register_group_admission_api
    from komari_bot.plugins.komari_help.api import register_help_api
    from komari_bot.plugins.komari_knowledge.api import register_knowledge_api
    from komari_bot.plugins.komari_management.api_runtime import (
        ManagementApiComponents,
    )
    from komari_bot.plugins.komari_management.managed_resources import (
        ManagedConfigResource,
    )
    from komari_bot.plugins.komari_memory.api import register_memory_api
    from komari_bot.plugins.komari_search.api import register_search_api
    from komari_bot.plugins.user_ban.api import register_user_ban_api

    parameters = inspect.signature(ManagementApiComponents).parameters
    missing = [name for name in ROULETTE_COMPONENT_FIELDS if name not in parameters]
    assert not missing, (
        "ManagementApiComponents 缺少 TSK-279 C2 字段: " + ", ".join(missing)
    )
    roulette = importlib.import_module(ROULETTE_PACKAGE)
    return ManagementApiComponents(
        register_knowledge_api=register_knowledge_api,
        knowledge_engine_getter=lambda: None,
        register_help_api=register_help_api,
        help_engine_getter=lambda: None,
        register_memory_api=register_memory_api,
        memory_service_getter=lambda: None,
        memory_redis_getter=lambda: None,
        register_agent_run_log_api=register_agent_run_log_api,
        agent_run_log_reader_getter=lambda: None,
        register_search_api=register_search_api,
        register_user_ban_api=register_user_ban_api,
        user_ban_service_getter=lambda: None,
        register_group_admission_api=register_group_admission_api,
        reply_fulfillment_service_getter=lambda: None,
        config_resources=(
            ManagedConfigResource(
                resource_id="komari_management",
                display_name="Komari Management",
                manager_getter=lambda: _FakeConfigManager(),
            ),
        ),
        prompt_resources=(),
        register_character_binding_repair_api=register_character_binding_repair_api,
        character_binding_repair_service_getter=lambda: None,
        register_roulette_management_api=required_attr(
            roulette, "register_roulette_management_api"
        ),
        roulette_observation_getter=required_attr(
            roulette, "get_roulette_observation"
        ),
    )
