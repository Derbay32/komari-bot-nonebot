"""TSK-279 Stage-C2 RED: roulette 管理控制面（status / inspect / rebuild）。

生产 ``komari_bot.plugins.komari_roulette.management_api`` 尚未落地，因此
除 green 探针外本文件为**预期 RED**（``ModuleNotFoundError`` / ``AttributeError``）。
这里冻结 Stage-C2 的对外契约：

* ``GET  /api/v2/komari-roulette/status``               —— ``roulette:read``
* ``POST /api/v2/komari-roulette/leaderboards/inspect`` —— ``roulette:read``
* ``POST /api/v2/komari-roulette/leaderboards/rebuild`` —— ``roulette:manage``
  且必须携带 ``X-Komari-Change-Reason`` 与 ``X-Request-ID``

控制面**只**暴露这三条路由，不提供 reset / 任意改分 / 预览令牌等绕过口。
只读面不写审计；rebuild 面经 ``management_audit_span`` 记录 started / succeeded，
审计最终写入失败不撤销已提交的重建事实，但 started 阶段失败必须零变更。
路线形态与真实存储的一致性验收见 ``test_tsk279_leaderboard_management_pg.py``。
"""

from __future__ import annotations

import ast
import builtins
import dataclasses
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI

from komari_bot.management.management_api import ManagementPrincipal
from komari_bot.management.management_audit import (
    ManagementAuditEvent,
    hash_management_target,
    management_audit_span,
)
from komari_bot.plugins.komari_roulette.observability import RouletteObservation

from .tsk279_management_support import (
    DISCREPANCY_CODES,
    ENTRY_RESPONSE_FIELDS,
    EXPECTED_ROUTES,
    FORBIDDEN_PATH_WORDS,
    FOREIGN_TOKEN,
    INSPECT_PATH,
    INSPECT_RESPONSE_FIELDS,
    MANAGER_TOKEN,
    READER_TOKEN,
    REBUILD_ACTION,
    REBUILD_PATH,
    REBUILD_RESOURCE,
    REBUILD_RESPONSE_FIELDS,
    ROULETTE_COMPONENT_FIELDS,
    ROULETTE_ERROR_CODES,
    ROULETTE_LIFECYCLE_MODULE,
    ROULETTE_PACKAGE,
    ROULETTE_STORAGE_MODULE,
    STATUS_LIFECYCLE_CASES,
    STATUS_PATH,
    STATUS_RESPONSE_FIELDS,
    T0,
    WILDCARD_TOKEN,
    FakeLeaderboardStorage,
    FakeSessionFactory,
    RecordingAuditRecorder,
    asgi_client,
    assert_error_envelope,
    auth_headers,
    build_control_plane,
    build_management_components,
    inspect_body,
    install_real_observation_owner,
    leaderboard_entry,
    load_management_module,
    load_symbol,
    make_inspection,
    management_credentials,
    rebuild_headers,
    registered_api_routes,
    required_attr,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ROULETTE_INIT_PATH = (
    PROJECT_ROOT / "komari_bot" / "plugins" / "komari_roulette" / "__init__.py"
)
ROULETTE_MANAGEMENT_PATH = (
    PROJECT_ROOT / "komari_bot" / "plugins" / "komari_roulette" / "management_api.py"
)

APP_ID = "tsk279-c2-app"
GROUP_OPENID = "tsk279-c2-group"
SECRET_LEAK = "SECRET-LEAK-9f2c4d"
C2_TOP_LEVEL_EXPORTS: tuple[str, ...] = (
    "get_roulette_observation",
    "register_roulette_management_api",
    "LeaderboardInspection",
)

TWO_ENTRIES = (
    leaderboard_entry(display_name="Aki", wins=1, last_won_at=T0),
    leaderboard_entry(display_name="Fumi", wins=2, last_won_at=T0),
)


def _source(path: Path) -> str:
    assert path.is_file(), f"缺少源文件: {path}"
    return path.read_text(encoding="utf-8")


def _module_all_exports(path: Path) -> set[str]:
    tree = ast.parse(_source(path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    return {str(value) for value in ast.literal_eval(node.value)}
    message = f"{path} 未声明顶层 __all__"
    raise AssertionError(message)


def _imported_modules(path: Path) -> Iterator[str]:
    tree = ast.parse(_source(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield "".join(["." * node.level, node.module or ""])


def _scripted_failure(name: str) -> BaseException:
    """Build a scripted storage failure; plain builtins cover generic errors."""

    module = importlib.import_module(ROULETTE_STORAGE_MODULE)
    error_type = getattr(module, name, None) or getattr(builtins, name)
    return error_type(SECRET_LEAK)


class _FakeDriver:
    def __init__(self, driver_type: str, server_app: FastAPI | None = None) -> None:
        self.type = driver_type
        self.server_app = server_app


class _FakeLogger:
    def __init__(self) -> None:
        self.info_messages: list[str] = []
        self.warning_messages: list[str] = []

    @staticmethod
    def _render(message: str, args: tuple[object, ...]) -> str:
        if not args:
            return message
        try:
            return message.format(*args)
        except Exception:
            try:
                return message % args
            except Exception:
                return " ".join([message, *(str(arg) for arg in args)])

    def info(self, message: str, *args: object) -> None:
        self.info_messages.append(self._render(message, args))

    def warning(self, message: str, *args: object) -> None:
        self.warning_messages.append(self._render(message, args))


# ---------------------------------------------------------------------------
# GREEN probes: the route module / top-level exports do not exist yet (RED),
# but the observing types and shared management seams must already be intact.
# ---------------------------------------------------------------------------


def test_green_probe_observation_and_shared_management_seams() -> None:
    observation = RouletteObservation(runtime_status="ready")
    assert set(observation.as_dict()) == STATUS_RESPONSE_FIELDS
    assert callable(hash_management_target)
    assert management_credentials()[0]["permissions"] == ["roulette:read"]


def test_roulette_management_module_declares_the_frozen_prefix() -> None:
    module = load_management_module()
    assert required_attr(module, "API_PREFIX") == "/api/v2/komari-roulette"
    assert INSPECT_PATH == "/api/v2/komari-roulette/leaderboards/inspect"
    assert REBUILD_PATH == "/api/v2/komari-roulette/leaderboards/rebuild"


def test_management_error_codes_are_the_frozen_closed_set() -> None:
    module = load_management_module()
    codes = required_attr(module, "ROULETTE_MANAGEMENT_ERROR_CODES")
    assert isinstance(codes, (set, frozenset, tuple)), type(codes)
    assert set(codes) == ROULETTE_ERROR_CODES


def test_roulette_package_exposes_the_c2_surface_at_top_level() -> None:
    exports = _module_all_exports(ROULETTE_INIT_PATH)
    missing = [name for name in C2_TOP_LEVEL_EXPORTS if name not in exports]
    assert not missing, f"顶层 __all__ 缺少 Stage-C2 跨插件暴露面: {missing}"
    package = importlib.import_module(ROULETTE_PACKAGE)
    for name in C2_TOP_LEVEL_EXPORTS:
        assert required_attr(package, name) is not None, name


# ---------------------------------------------------------------------------
# Route surface
# ---------------------------------------------------------------------------


async def test_exact_route_surface_and_idempotent_registration() -> None:
    plane = build_control_plane(observation=None)
    routes = registered_api_routes(plane.app)
    assert routes == EXPECTED_ROUTES, routes
    for path, _method in EXPECTED_ROUTES:
        assert not any(word in path for word in FORBIDDEN_PATH_WORDS), path

    plane.module.register_roulette_management_api(
        plane.app, **plane.registration_kwargs
    )
    assert registered_api_routes(plane.app) == EXPECTED_ROUTES


async def test_control_plane_needs_no_plugin_enable_or_admission_gate() -> None:
    """控制面注册不读动态配置、不过群准入闸门（观测/运维通道）。"""

    plane = build_control_plane(observation=None)
    assert registered_api_routes(plane.app) == EXPECTED_ROUTES
    async with asgi_client(plane.app) as client:
        denied = await client.get(STATUS_PATH, headers=auth_headers(FOREIGN_TOKEN))
        assert denied.status_code == 403, denied.text


def test_roulette_management_module_imports_no_forbidden_gate() -> None:
    imported = list(_imported_modules(ROULETTE_MANAGEMENT_PATH))
    banned = (
        "komari_bot.plugins.group_admission",
        "komari_bot.plugins.komari_roulette.config_schema",
        "komari_bot.plugins.komari_management",
    )
    offenders = [
        name
        for name in imported
        if any(name == prefix or name.startswith(f"{prefix}.") for prefix in banned)
    ]
    assert not offenders, f"控制面路由不得依赖门控/管理插件内部模块: {offenders}"


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


async def test_status_projects_the_frozen_snapshot_and_permission_matrix() -> None:
    observation = RouletteObservation(
        runtime_status="ready",
        runtime_reason="policy_admitted",
        latest_scan={"scanned": 2, "advanced": 2, "skipped_restricted": 0, "failed": 0},
        latest_cleanup={
            "receipts_deleted": 1,
            "games_deleted": 0,
            "results_deleted": 0,
            "more_pending": False,
        },
        pending_receipts=1,
        fault_counts=(("storage_unavailable", 1),),
    )
    plane = build_control_plane(observation=observation)
    async with asgi_client(plane.app) as client:
        for token in (READER_TOKEN, MANAGER_TOKEN, WILDCARD_TOKEN):
            response = await client.get(STATUS_PATH, headers=auth_headers(token))
            assert response.status_code == 200, (token, response.text)
            body = response.json()
            assert set(body) == STATUS_RESPONSE_FIELDS, body
            assert body == observation.as_dict()
        foreign = await client.get(STATUS_PATH, headers=auth_headers(FOREIGN_TOKEN))
        assert foreign.status_code == 403, foreign.text
        anonymous = await client.get(STATUS_PATH)
        assert anonymous.status_code == 401, anonymous.text
    assert plane.audit.events == []


@pytest.mark.parametrize(("runtime_status", "runtime_reason"), STATUS_LIFECYCLE_CASES)
async def test_status_returns_every_lifecycle_state_through_the_real_getter(
    monkeypatch: pytest.MonkeyPatch,
    runtime_status: str,
    runtime_reason: str,
) -> None:
    """控制面对 ready/disabled/failed 一律 200：业务状态不由 HTTP 层拒绝。

    观测值由**真实**默认 getter（``get_roulette_observation``）读取一个结构性
    owner 产生，因此运行时状态归一化与快照投影都是生产代码路径。
    """

    install_real_observation_owner(
        monkeypatch, status=runtime_status, reason=runtime_reason
    )
    plane = build_control_plane()
    async with asgi_client(plane.app) as client:
        for token in (READER_TOKEN, MANAGER_TOKEN, WILDCARD_TOKEN):
            response = await client.get(STATUS_PATH, headers=auth_headers(token))
            assert response.status_code == 200, (runtime_status, token, response.text)
            body = response.json()
            assert set(body) == STATUS_RESPONSE_FIELDS, body
            assert body["runtime_status"] == runtime_status, body
            assert body["runtime_reason"] == runtime_reason, body
        foreign = await client.get(STATUS_PATH, headers=auth_headers(FOREIGN_TOKEN))
        assert foreign.status_code == 403, foreign.text
    assert plane.audit.events == []


async def test_status_fails_closed_when_no_application_owns_the_runtime() -> None:
    """未装配 owner ⇒ 503 ``roulette_status_unavailable``，绝不伪造 ready。"""

    stop = load_symbol(ROULETTE_LIFECYCLE_MODULE, "stop_roulette_application")
    await stop()

    plane = build_control_plane(observation=None)
    async with asgi_client(plane.app) as client:
        response = await client.get(STATUS_PATH, headers=auth_headers(READER_TOKEN))
    assert_error_envelope(response, 503, "roulette_status_unavailable")


class _ProjectionFailureObservation:
    """Observation collaborator whose safe snapshot projection raises.

    ``register_roulette_management_api`` accepts any observation object, so the
    control plane must fail closed when that collaborator cannot project a
    snapshot instead of letting the raw exception escape as a default 500.  The
    deliberately non-``RouletteObservation`` collaborator is legal for the
    ``object``-typed seam and does not force production to widen its types.
    """

    __slots__ = ("calls", "canary")

    def __init__(self, canary: str) -> None:
        self.canary = canary
        self.calls = 0

    def as_dict(self) -> dict[str, object]:
        self.calls += 1
        raise RuntimeError(self.canary)


async def test_status_projection_failure_fails_closed_without_leaking() -> None:
    """快照投影协作者抛错 ⇒ 503 ``roulette_status_unavailable``，不泄正文。

    状态路由当前只在 getter 外包 try，``as_dict()`` 在 try 外；协作者抛错会落到
    FastAPI 默认 500（ASGI 传输默认还会重抛）。控制面必须把「快照投影失败」纳入
    固定错误外壳，且响应体不得回显 canary。
    """

    collaborator = _ProjectionFailureObservation(SECRET_LEAK)
    plane = build_control_plane(observation=collaborator)
    async with asgi_client(
        plane.app,
        raise_app_exceptions=False,
    ) as client:
        response = await client.get(STATUS_PATH, headers=auth_headers(READER_TOKEN))
    assert_error_envelope(
        response,
        503,
        "roulette_status_unavailable",
        leaked=[SECRET_LEAK],
    )
    assert collaborator.calls == 1
    assert plane.audit.events == []


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------


async def test_inspect_projects_safe_leaderboard_and_permission_matrix() -> None:
    inspection = make_inspection(
        app_id=APP_ID,
        group_openid=GROUP_OPENID,
        cached=TWO_ENTRIES,
        completed=TWO_ENTRIES,
    )
    plane = build_control_plane(
        observation=None,
        storage=FakeLeaderboardStorage(inspection=inspection),
    )
    async with asgi_client(plane.app) as client:
        for token in (READER_TOKEN, MANAGER_TOKEN, WILDCARD_TOKEN):
            response = await client.post(
                INSPECT_PATH,
                headers=auth_headers(token),
                json=inspect_body(APP_ID, GROUP_OPENID),
            )
            assert response.status_code == 200, (token, response.text)
            body = response.json()
            assert set(body) == INSPECT_RESPONSE_FIELDS, body
            assert body["app_id"] == APP_ID
            assert body["group_openid"] == GROUP_OPENID
            assert body["consistent"] is True
            assert body["cached_entry_count"] == 2
            assert body["completed_entry_count"] == 2
            assert body["cached_total_wins"] == 3
            assert body["completed_total_wins"] == 3
            assert body["discrepancy_codes"] == []
            assert len(body["entries"]) == 2
            for entry in body["entries"]:
                assert set(entry) == ENTRY_RESPONSE_FIELDS, entry
                assert "member_openid" not in json.dumps(entry)
        foreign = await client.post(
            INSPECT_PATH,
            headers=auth_headers(FOREIGN_TOKEN),
            json=inspect_body(APP_ID, GROUP_OPENID),
        )
        assert foreign.status_code == 403, foreign.text
    assert plane.storage.rebuilds == []
    assert plane.audit.events == []


async def test_inspect_reports_discrepancies_without_mutating_the_cache() -> None:
    inspection = make_inspection(
        app_id=APP_ID,
        group_openid=GROUP_OPENID,
        cached=(TWO_ENTRIES[0],),
        completed=TWO_ENTRIES,
        discrepancy_codes=("missing_cached_row", "wins_mismatch"),
    )
    assert set(inspection.discrepancy_codes) <= DISCREPANCY_CODES
    plane = build_control_plane(
        observation=None,
        storage=FakeLeaderboardStorage(inspection=inspection),
    )
    async with asgi_client(plane.app) as client:
        response = await client.post(
            INSPECT_PATH,
            headers=auth_headers(READER_TOKEN),
            json=inspect_body(APP_ID, GROUP_OPENID),
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["consistent"] is False
    assert body["discrepancy_codes"] == ["missing_cached_row", "wins_mismatch"]
    assert plane.storage.rebuilds == []
    assert plane.audit.events == []


@pytest.mark.parametrize(
    ("error", "code"),
    [
        ("StorageUnavailableError", "roulette_storage_unavailable"),
        ("AggregateCorruptError", "roulette_aggregate_corrupt"),
        ("RuntimeError", "roulette_storage_unavailable"),
    ],
)
async def test_inspect_failures_use_the_fixed_error_envelope(
    error: str,
    code: str,
) -> None:
    plane = build_control_plane(
        observation=None,
        storage=FakeLeaderboardStorage(inspect_error=_scripted_failure(error)),
    )
    async with asgi_client(plane.app) as client:
        response = await client.post(
            INSPECT_PATH,
            headers=auth_headers(READER_TOKEN),
            json=inspect_body(APP_ID, GROUP_OPENID),
        )
    assert_error_envelope(response, 503, code, leaked=[SECRET_LEAK])
    # 固定 503 外壳不是「空排行榜」200：不得携带任何排行榜字段。
    body = response.json()
    assert set(body) == {"detail"}, body
    assert "entries" not in body


async def test_inspect_and_rebuild_forbid_extra_fields_and_empty_identifiers() -> None:
    plane = build_control_plane(observation=None)
    cases: tuple[tuple[str, dict[str, str]], ...] = (
        (INSPECT_PATH, auth_headers(READER_TOKEN)),
        (REBUILD_PATH, rebuild_headers(MANAGER_TOKEN)),
    )
    async with asgi_client(plane.app) as client:
        for path, headers in cases:
            extra = await client.post(
                path,
                headers=headers,
                json={**inspect_body(APP_ID, GROUP_OPENID), "wins": 99},
            )
            assert extra.status_code == 422, (path, extra.text)
            for body in (
                {"app_id": "", "group_openid": GROUP_OPENID},
                {"app_id": APP_ID, "group_openid": ""},
                {"app_id": APP_ID},
                {"group_openid": GROUP_OPENID},
                {},
            ):
                bad = await client.post(path, headers=headers, json=body)
                assert bad.status_code == 422, (path, body, bad.text)
    assert plane.storage.rebuilds == []
    assert plane.audit.events == []


# ---------------------------------------------------------------------------
# rebuild
# ---------------------------------------------------------------------------


async def test_rebuild_requires_manage_reason_and_request_id() -> None:
    plane = build_control_plane(observation=None)
    body = inspect_body(APP_ID, GROUP_OPENID)
    async with asgi_client(plane.app) as client:
        no_reason = await client.post(
            REBUILD_PATH,
            headers=auth_headers(MANAGER_TOKEN),
            json=body,
        )
        assert no_reason.status_code == 400, no_reason.text
        blank_reason = await client.post(
            REBUILD_PATH,
            headers={**auth_headers(MANAGER_TOKEN), "X-Komari-Change-Reason": "  "},
            json=body,
        )
        assert blank_reason.status_code == 400, blank_reason.text
        long_reason = await client.post(
            REBUILD_PATH,
            headers=rebuild_headers(MANAGER_TOKEN, reason="x" * 201),
            json=body,
        )
        assert long_reason.status_code == 422, long_reason.text
        no_request_id = await client.post(
            REBUILD_PATH,
            headers={
                **auth_headers(MANAGER_TOKEN),
                "X-Komari-Change-Reason": "tsk279-c2-verify-leaderboard",
            },
            json=body,
        )
        assert no_request_id.status_code == 400, no_request_id.text
        bad_request_id = await client.post(
            REBUILD_PATH,
            headers=rebuild_headers(MANAGER_TOKEN, request_id="bad id!"),
            json=body,
        )
        assert bad_request_id.status_code == 422, bad_request_id.text

        reader_only = await client.post(
            REBUILD_PATH,
            headers=rebuild_headers(READER_TOKEN),
            json=body,
        )
        assert reader_only.status_code == 403, reader_only.text
        foreign = await client.post(
            REBUILD_PATH,
            headers=rebuild_headers(FOREIGN_TOKEN),
            json=body,
        )
        assert foreign.status_code == 403, foreign.text
    assert plane.storage.rebuilds == []
    assert plane.audit.events == []


async def test_rebuild_commits_once_and_records_safe_audit_events() -> None:
    inspection = make_inspection(
        app_id=APP_ID,
        group_openid=GROUP_OPENID,
        cached=TWO_ENTRIES,
        completed=TWO_ENTRIES,
    )
    storage = FakeLeaderboardStorage(inspection=inspection)
    audit = RecordingAuditRecorder(probe=lambda: list(storage.rebuilds))
    plane = build_control_plane(
        observation=None,
        storage=storage,
        audit=audit,
    )
    async with asgi_client(plane.app) as client:
        response = await client.post(
            REBUILD_PATH,
            headers=rebuild_headers(MANAGER_TOKEN, request_id="tsk279-rebuild-0001"),
            json=inspect_body(APP_ID, GROUP_OPENID),
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == REBUILD_RESPONSE_FIELDS, body
    assert body["app_id"] == APP_ID
    assert body["group_openid"] == GROUP_OPENID
    assert body["consistent"] is True
    assert body["entry_count"] == 2
    assert body["total_wins"] == 3

    assert plane.storage.rebuilds == [(APP_ID, GROUP_OPENID)]
    assert plane.session_factory.commits == 1
    assert plane.session_factory.rollbacks == 0

    started, succeeded = audit.events
    assert [event.outcome for event in audit.events] == ["started", "succeeded"]
    assert started.action == REBUILD_ACTION
    assert succeeded.action == REBUILD_ACTION
    for event in (started, succeeded):
        assert event.resource == REBUILD_RESOURCE
        assert event.request_id == "tsk279-rebuild-0001"
        assert event.field_name is None
        assert event.target_hash == hash_management_target(APP_ID, GROUP_OPENID)
    assert succeeded.status_code == 200
    assert succeeded.metadata["entry_count"] == 2
    assert succeeded.metadata["total_wins"] == 3
    assert succeeded.metadata["result_code"] == "rebuilt"
    assert audit.probes[0] == [], "started 必须先于排行榜变更"
    assert audit.probes[1] == [(APP_ID, GROUP_OPENID)]


async def test_rebuild_audit_final_failure_keeps_the_committed_fact() -> None:
    """审计 succeeded 写入失败只告警，不回滚已提交的重建事实。"""

    inspection = make_inspection(
        app_id=APP_ID,
        group_openid=GROUP_OPENID,
        cached=TWO_ENTRIES,
        completed=TWO_ENTRIES,
    )
    storage = FakeLeaderboardStorage(inspection=inspection)
    audit = RecordingAuditRecorder(fail_on_outcome="succeeded")
    plane = build_control_plane(observation=None, storage=storage, audit=audit)
    async with asgi_client(plane.app) as client:
        response = await client.post(
            REBUILD_PATH,
            headers=rebuild_headers(MANAGER_TOKEN),
            json=inspect_body(APP_ID, GROUP_OPENID),
        )
    assert response.status_code == 200, response.text
    assert plane.storage.rebuilds == [(APP_ID, GROUP_OPENID)]
    assert plane.session_factory.commits == 1
    assert [event.outcome for event in audit.events] == ["started", "succeeded"]


async def test_rebuild_started_audit_failure_leaves_no_change() -> None:
    """审计 started 写入失败必须中止在变更之前（零 rebuild / 零 commit）。"""

    inspection = make_inspection(
        app_id=APP_ID,
        group_openid=GROUP_OPENID,
        cached=TWO_ENTRIES,
        completed=TWO_ENTRIES,
    )
    storage = FakeLeaderboardStorage(inspection=inspection)
    audit = RecordingAuditRecorder(fail_on_outcome="started")
    plane = build_control_plane(observation=None, storage=storage, audit=audit)
    async with asgi_client(plane.app) as client:
        response = await client.post(
            REBUILD_PATH,
            headers=rebuild_headers(MANAGER_TOKEN),
            json=inspect_body(APP_ID, GROUP_OPENID),
        )
    assert response.status_code != 200, response.text
    assert plane.storage.rebuilds == []
    assert plane.session_factory.commits == 0
    assert [event.outcome for event in audit.events] == ["started"]


@pytest.mark.parametrize(
    ("error", "code"),
    [
        ("AggregateCorruptError", "roulette_aggregate_corrupt"),
        ("RuntimeError", "roulette_storage_unavailable"),
    ],
)
async def test_rebuild_failures_use_the_fixed_error_envelope(
    error: str,
    code: str,
) -> None:
    plane = build_control_plane(
        observation=None,
        storage=FakeLeaderboardStorage(rebuild_error=_scripted_failure(error)),
    )
    async with asgi_client(plane.app) as client:
        response = await client.post(
            REBUILD_PATH,
            headers=rebuild_headers(MANAGER_TOKEN),
            json=inspect_body(APP_ID, GROUP_OPENID),
        )
    assert_error_envelope(response, 503, code, leaked=[SECRET_LEAK])
    assert set(response.json()) == {"detail"}
    assert plane.session_factory.commits == 0
    assert plane.session_factory.commit_attempts == 0


async def test_rebuild_commit_failure_fails_closed_without_partial_state() -> None:
    """commit 抛 SQL/连接错误（已知失败）⇒ 固定 503，零成功提交、零重试。

    用例只模拟 ``commit()`` 在应用任何状态前抛出的**已知**失败，因此可以断言没有
    成功提交；它**不**宣称真实网络 commit 结果未知时必然回滚。生产若在
    ``session.commit()`` 抛出后落入 generic 500，会把「运维不可用」误报成内部错误。
    """

    inspection = make_inspection(
        app_id=APP_ID,
        group_openid=GROUP_OPENID,
        cached=TWO_ENTRIES,
        completed=TWO_ENTRIES,
    )
    storage = FakeLeaderboardStorage(inspection=inspection)
    sessions = FakeSessionFactory(commit_error=RuntimeError(SECRET_LEAK))
    audit = RecordingAuditRecorder()
    plane = build_control_plane(
        observation=None,
        storage=storage,
        session_factory=sessions,
        audit=audit,
    )
    async with asgi_client(plane.app) as client:
        response = await client.post(
            REBUILD_PATH,
            headers=rebuild_headers(
                MANAGER_TOKEN,
                request_id="tsk279-rebuild-commit-fail",
            ),
            json=inspect_body(APP_ID, GROUP_OPENID),
        )
    assert_error_envelope(
        response,
        503,
        "roulette_storage_unavailable",
        leaked=[SECRET_LEAK],
    )
    # 已知失败的 commit：尝试一次、成功 0 ⇒ 没有部分提交。
    assert sessions.commit_attempts == 1
    assert sessions.commits == 0
    # 不重试 rebuild：恰好一次重建尝试、一个已退出的 session。
    assert storage.rebuilds == [(APP_ID, GROUP_OPENID)]
    assert len(sessions.sessions) == 1
    assert sessions.sessions[0].closed is True
    # 审计绝不写 succeeded。
    outcomes = [event.outcome for event in audit.events]
    assert "succeeded" not in outcomes, outcomes
    assert outcomes[0] == "started", outcomes


# ---------------------------------------------------------------------------
# Management assembly (formal wiring)
# ---------------------------------------------------------------------------


def test_management_components_expose_the_roulette_fields() -> None:
    components = build_management_components()
    names = {field.name for field in dataclasses.fields(components)}
    missing = [name for name in ROULETTE_COMPONENT_FIELDS if name not in names]
    assert not missing, f"ManagementApiComponents 缺少 Stage-C2 字段: {missing}"


async def test_shared_audit_span_started_is_fail_closed_and_final_is_swallowed() -> None:
    """生产 ``management_audit_span`` 语义：``started`` 失败中止，final 失败被吞。

    C2 的 rebuild 审计沿用该共享接缝，所以把它的真实语义冻结在用例里：任何新
    入口都不得另造一套绕过共享审计的机制。此探针不依赖 C2 seam。
    """

    principal = ManagementPrincipal(
        operator_id="roulette-manager",
        permissions=frozenset({"roulette:manage"}),
    )
    observed: list[str] = []
    body_ran: list[str] = []

    async def failing_started(event: ManagementAuditEvent) -> None:
        observed.append(event.outcome)
        message = "started audit unavailable"
        raise RuntimeError(message)

    with pytest.raises(RuntimeError, match="started audit unavailable"):
        async with management_audit_span(
            principal=principal,
            request_id="tsk279-audit-started",
            reason="tsk279-c2-verify-leaderboard",
            action=REBUILD_ACTION,
            resource=REBUILD_RESOURCE,
            recorder=failing_started,
        ):
            body_ran.append("started")
    assert observed == ["started"]
    assert body_ran == []

    observed.clear()

    async def failing_final(event: ManagementAuditEvent) -> None:
        observed.append(event.outcome)
        if event.outcome == "succeeded":
            final_message = "final audit unavailable"
            raise RuntimeError(final_message)

    async with management_audit_span(
        principal=principal,
        request_id="tsk279-audit-succeeded",
        reason="tsk279-c2-verify-leaderboard",
        action=REBUILD_ACTION,
        resource=REBUILD_RESOURCE,
        recorder=failing_final,
    ):
        body_ran.append("succeeded")
    assert observed == ["started", "succeeded"]
    assert body_ran == ["succeeded"]


async def test_real_management_assembly_mounts_roulette_routes() -> None:
    from komari_bot.plugins.komari_management.api_runtime import (
        register_management_api_for_driver,
    )

    observation = RouletteObservation(
        runtime_status="ready",
        runtime_reason="policy_admitted",
    )

    def _loader() -> Any:
        return dataclasses.replace(
            build_management_components(),
            roulette_observation_getter=lambda: observation,
        )

    api_app = FastAPI(docs_url="/api/docs", openapi_url="/api/openapi.json")
    logger = _FakeLogger()
    config = SimpleNamespace(
        plugin_enable=True,
        api_credentials=management_credentials(),
        api_allowed_origins=[],
    )
    registered = register_management_api_for_driver(
        driver=_FakeDriver("fastapi", api_app),
        config=config,
        component_loader=_loader,
        logger=logger,
    )
    assert registered is True
    assert logger.warning_messages == []
    assert registered_api_routes(api_app) >= EXPECTED_ROUTES

    async with asgi_client(api_app) as client:
        response = await client.get(STATUS_PATH, headers=auth_headers(READER_TOKEN))
        assert response.status_code == 200, response.text
        assert response.json() == observation.as_dict()
        denied = await client.get(STATUS_PATH, headers=auth_headers(FOREIGN_TOKEN))
        assert denied.status_code == 403, denied.text


def test_audit_event_projection_never_carries_identity() -> None:
    event = ManagementAuditEvent(
        timestamp="2026-09-20T12:00:00+00:00",
        request_id="tsk279-rebuild-0001",
        operator_id="roulette-manager",
        action=REBUILD_ACTION,
        resource=REBUILD_RESOURCE,
        reason="tsk279-c2-verify-leaderboard",
        outcome="succeeded",
        target_hash=hash_management_target(APP_ID, GROUP_OPENID),
        metadata={"entry_count": 2, "total_wins": 3, "result_code": "rebuilt"},
    )
    rendered = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True)
    assert APP_ID not in rendered
    assert GROUP_OPENID not in rendered
