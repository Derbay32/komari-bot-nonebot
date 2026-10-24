"""TSK-182 判定诊断管理 API 验收：健康、元数据、受权正文、审计与禁止缓存。

断言只穿过 HTTP 可观测结果（状态码、响应体、响应头）与既有管理审计事件，
不绑定私有辅助函数；健康查询不访问仓储，元数据查询永不包含正文/预览。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

from fastapi import FastAPI

from komari_bot.decision.jev import (
    DecisionHealthSnapshot,
    DecisionLogMetadata,
    DecisionResult,
    DecisionUsage,
    EventIdentity,
    PhaseDurations,
    ReferenceSummary,
    ReplyChoice,
    RetentionChoice,
)
from komari_bot.management.management_audit import hash_management_target
from komari_bot.plugins.komari_decision.management_api import (
    BODY_READ_ACTION,
    BODY_READ_REASON,
    DECISION_RESOURCE,
    create_decision_router,
)
from komari_bot.plugins.komari_decision.repositories.decision_log_repository import (
    DecisionLogBody,
    DecisionLogPage,
    DecisionLogStorageUnavailable,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from nonebug import App

    from komari_bot.management.management_audit import ManagementAuditEvent

API_PREFIX = "/api/v2/komari-decision"
NO_STORE = "no-store"

_READ_TOKEN = "decision-read-token-0001"
_BODY_TOKEN = "decision-body-token-0001"
_ADMIN_TOKEN = "decision-admin-token-0001"
_OTHER_TOKEN = "decision-other-token-0002"

_CREDENTIALS = (
    {
        "credential_id": "decision-reader",
        "token": _READ_TOKEN,
        "permissions": ["decision:read"],
    },
    {
        "credential_id": "decision-body-reader",
        "token": _BODY_TOKEN,
        "permissions": ["decision_logs:read"],
    },
    {
        "credential_id": "decision-admin",
        "token": _ADMIN_TOKEN,
        "permissions": ["*"],
    },
    {
        "credential_id": "scene-operator",
        "token": _OTHER_TOKEN,
        "permissions": ["scene:read"],
    },
)

_READ_HEADERS = {"Authorization": f"Bearer {_READ_TOKEN}"}
_BODY_HEADERS = {"Authorization": f"Bearer {_BODY_TOKEN}"}
_ADMIN_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_OTHER_HEADERS = {"Authorization": f"Bearer {_OTHER_TOKEN}"}

_DECISION_ID = "00000000-0000-0000-0000-00000000c0de"
_BODY_SECRET = "正文不得进入审计或元数据"
_FIXED_422_DETAIL = "判定日志查询参数无效"
_FIXED_503_DETAIL = "判定日志存储暂不可用"
_FIXED_404_DETAIL = "判定日志不存在或已清理"
_FIXED_IDENTIFIER_DETAIL = "判定日志标识必须是 UUID"


# ------------------------------ 替身 ------------------------------


class _RepositorySpy:
    """判定日志仓储替身：记录调用、可按方法注入存储失败。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.page = DecisionLogPage(items=(), total=0, page=1, page_size=50)
        self.metadata: DecisionLogMetadata | None = None
        self.body: DecisionLogBody | None = None
        self.failures: dict[str, Exception] = {}

    def _enter(self, name: str, **kwargs: Any) -> None:
        self.calls.append((name, kwargs))
        failure = self.failures.get(name)
        if failure is not None:
            raise failure

    def called(self, name: str) -> bool:
        return any(call[0] == name for call in self.calls)

    async def record(self, *_args: Any, **_kwargs: Any) -> None:
        self._enter("record")

    async def list_metadata(self, **kwargs: Any) -> DecisionLogPage:
        self._enter("list_metadata", **kwargs)
        return self.page

    async def get_metadata(self, decision_id: str) -> DecisionLogMetadata | None:
        self._enter("get_metadata", decision_id=decision_id)
        return self.metadata

    async def get_body(self, decision_id: str) -> DecisionLogBody | None:
        self._enter("get_body", decision_id=decision_id)
        return self.body

    async def prune(self, *, limit: int = 500) -> int:
        self._enter("prune", limit=limit)
        return 0


class _HealthProvider:
    """同步健康快照提供者：记录调用次数。"""

    def __init__(self, snapshot: DecisionHealthSnapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0
        self.failure: Exception | None = None

    def __call__(self) -> DecisionHealthSnapshot:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return self.snapshot


class _AuditCollector:
    """审计记录器替身：可让起始事件失败以验证拒绝读取。"""

    def __init__(self, start_failure: Exception | None = None) -> None:
        self.events: list[ManagementAuditEvent] = []
        self.start_failure = start_failure

    async def __call__(self, event: ManagementAuditEvent) -> None:
        if self.start_failure is not None and event.outcome == "started":
            raise self.start_failure
        self.events.append(event)

    @property
    def outcomes(self) -> list[str]:
        return [event.outcome for event in self.events]


@asynccontextmanager
async def _serve(
    nonebug_app: App,
    *,
    repository: _RepositorySpy,
    health_provider: _HealthProvider,
    audit: _AuditCollector,
) -> AsyncIterator[Any]:
    api_app = FastAPI()
    api_app.include_router(
        create_decision_router(
            api_token=_CREDENTIALS,
            health_provider=health_provider,
            log_repository=cast("Any", repository),
            audit_recorder=audit,
        )
    )
    async with nonebug_app.test_server(asgi=cast("Any", api_app)) as ctx:
        yield ctx.get_client()


# ------------------------------ 字面量 ------------------------------


def _health_snapshot(
    runtime_status: str = "ready",
    *,
    observed_at: datetime | None = None,
) -> DecisionHealthSnapshot:
    return DecisionHealthSnapshot(
        runtime_status=cast("Any", runtime_status),
        reason_code="ready",
        last_request_status="unknown",
        last_success_at=None,
        last_failure_at=None,
        last_failure_reason_code=None,
        consecutive_failures=0,
        in_flight_count=None,
        queued_count=None,
        observed_at=observed_at or datetime(2026, 10, 24, 8, 30, tzinfo=UTC),
    )


def _metadata(decision_id: str = _DECISION_ID) -> DecisionLogMetadata:
    created_at = datetime(2026, 10, 24, 8, 0, tzinfo=UTC)
    return DecisionLogMetadata(
        decision_id=decision_id,
        event=EventIdentity(
            protocol="onebot_v11",
            account_id="acct-1",
            group_id="group-1",
            message_id="msg-1",
        ),
        created_at=created_at,
        finished_at=created_at + timedelta(seconds=2),
        result=DecisionResult(
            decision_id=decision_id,
            status="success",
            reason_code="classified",
            route="chat",
            force_reply=True,
            reply_choice=ReplyChoice(
                value="required",
                confidence=0.9,
                probabilities=(("required", 0.9), ("optional", 0.1)),
            ),
            retention_choice=RetentionChoice(
                value="candidate",
                confidence=0.6,
                probabilities=(("candidate", 0.6), ("noise", 0.4)),
            ),
            summary_probability=0.05,
            effective_reply="required",
            retain_recent=True,
            memory_candidate=True,
            optional_selected=None,
            references=ReferenceSummary(
                complete=True, resolved_count=1, missing_count=0
            ),
            durations=PhaseDurations(
                queue_ms=5.0, references_ms=None, request_ms=120.0
            ),
            usage=DecisionUsage(input_tokens=None, output_tokens=None),
            config_revision=3,
            prompt_revision=2,
            requested_model="jev-requested",
            actual_model="jev-actual",
        ),
    )


def _all_keys(value: Any) -> set[str]:
    """递归收集响应体中的全部键名。"""
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(str(key))
            keys |= _all_keys(item)
    elif isinstance(value, list):
        for item in value:
            keys |= _all_keys(item)
    return keys


# ------------------------------ 鉴权与权限矩阵 ------------------------------


async def test_authentication_and_permission_matrix(app: App) -> None:
    """AC：401/403 与只读/正文/admin 的权限矩阵；未授权不访问仓储。"""
    detail_path = f"{API_PREFIX}/decisions/{_DECISION_ID}"
    body_path = f"{detail_path}/body"
    repository = _RepositorySpy()
    repository.page = DecisionLogPage(
        items=(_metadata(),), total=1, page=1, page_size=50
    )
    repository.metadata = _metadata()
    repository.body = DecisionLogBody(decision_id=_DECISION_ID, body={"x": 1})

    async with _serve(
        app,
        repository=repository,
        health_provider=_HealthProvider(_health_snapshot()),
        audit=_AuditCollector(),
    ) as client:
        for path in (f"{API_PREFIX}/health", f"{API_PREFIX}/decisions", detail_path):
            anonymous = await client.get(path)
            assert anonymous.status_code == 401, path
            assert anonymous.headers["cache-control"] == NO_STORE
            forged = await client.get(
                path, headers={"Authorization": "Bearer not-a-real-token-0001"}
            )
            assert forged.status_code == 401, path
            assert forged.headers["cache-control"] == NO_STORE
            forbidden = await client.get(path, headers=_OTHER_HEADERS)
            assert forbidden.status_code == 403, path
            assert forbidden.headers["cache-control"] == NO_STORE
            body_only = await client.get(path, headers=_BODY_HEADERS)
            assert body_only.status_code == 403, path
            assert body_only.headers["cache-control"] == NO_STORE
            for headers in (_READ_HEADERS, _ADMIN_HEADERS):
                allowed = await client.get(path, headers=headers)
                assert allowed.status_code == 200, path
                assert allowed.headers["cache-control"] == NO_STORE

        for headers in (_READ_HEADERS, _OTHER_HEADERS):
            denied = await client.get(body_path, headers=headers)
            assert denied.status_code == 403
            assert denied.headers["cache-control"] == NO_STORE
        anonymous_body = await client.get(body_path)
        assert anonymous_body.status_code == 401
        assert anonymous_body.headers["cache-control"] == NO_STORE

        assert not repository.called("get_body"), "未授权的正文请求不得访问仓储"

        for headers in (_BODY_HEADERS, _ADMIN_HEADERS):
            granted = await client.get(body_path, headers=headers)
            assert granted.status_code == 200
            assert granted.json() == {"decision_id": _DECISION_ID, "body": {"x": 1}}
            assert granted.headers["cache-control"] == NO_STORE


async def test_unauthorized_requests_never_touch_repository(app: App) -> None:
    """AC：鉴权失败不进入仓储（元数据与正文路径都一样）。"""
    repository = _RepositorySpy()

    async with _serve(
        app,
        repository=repository,
        health_provider=_HealthProvider(_health_snapshot()),
        audit=_AuditCollector(),
    ) as client:
        await client.get(f"{API_PREFIX}/decisions", headers=_OTHER_HEADERS)
        await client.get(
            f"{API_PREFIX}/decisions/{_DECISION_ID}", headers=_BODY_HEADERS
        )
        await client.get(
            f"{API_PREFIX}/decisions/{_DECISION_ID}/body", headers=_READ_HEADERS
        )

    assert repository.calls == []


# ------------------------------ 健康 ------------------------------


async def test_health_serializes_passive_snapshot_for_every_status(app: App) -> None:
    """AC：ready/disabled/failed 都是 200；无观测证据的字段为 null。"""
    for runtime_status in ("ready", "disabled", "failed"):
        provider = _HealthProvider(_health_snapshot(runtime_status))
        repository = _RepositorySpy()
        async with _serve(
            app,
            repository=repository,
            health_provider=provider,
            audit=_AuditCollector(),
        ) as client:
            response = await client.get(
                f"{API_PREFIX}/health", headers=_READ_HEADERS
            )

        assert response.status_code == 200, runtime_status
        assert response.headers["cache-control"] == NO_STORE
        payload = response.json()
        assert payload == {
            "runtime_status": runtime_status,
            "reason_code": "ready",
            "last_request_status": "unknown",
            "last_success_at": None,
            "last_failure_at": None,
            "last_failure_reason_code": None,
            "consecutive_failures": 0,
            "in_flight_count": None,
            "queued_count": None,
            "observed_at": "2026-10-24T08:30:00Z",
        }
        assert provider.calls == 1, "健康查询只读取一次快照"
        assert repository.calls == [], "健康查询不得访问仓储"


async def test_health_never_opens_a_database_session(app: App) -> None:
    """AC：健康查询不产生任何数据库访问（会话工厂被调用即失败）。"""
    from komari_bot.plugins.komari_decision.repositories.decision_log_repository import (
        DecisionLogRepository,
    )

    def _exploding_session_factory() -> Any:
        raise AssertionError("健康查询不得访问判定日志存储")

    exploding_repository = DecisionLogRepository(
        _exploding_session_factory,
        lambda: 7,
    )
    async with _serve(
        app,
        repository=cast("Any", exploding_repository),
        health_provider=_HealthProvider(_health_snapshot("failed")),
        audit=_AuditCollector(),
    ) as client:
        response = await client.get(f"{API_PREFIX}/health", headers=_READ_HEADERS)

    assert response.status_code == 200
    assert response.json()["runtime_status"] == "failed"
    assert response.headers["cache-control"] == NO_STORE


async def test_health_provider_failure_returns_fixed_503(app: App) -> None:
    """AC：快照读取失败返回固定 503，不泄露异常正文。"""
    provider = _HealthProvider(_health_snapshot())
    provider.failure = RuntimeError("internal-detail-should-not-leak")

    async with _serve(
        app,
        repository=_RepositorySpy(),
        health_provider=provider,
        audit=_AuditCollector(),
    ) as client:
        response = await client.get(f"{API_PREFIX}/health", headers=_READ_HEADERS)

    assert response.status_code == 503
    assert response.headers["cache-control"] == NO_STORE
    assert "internal-detail-should-not-leak" not in response.text
    assert provider.calls == 1


# ------------------------------ 元数据 ------------------------------


async def test_metadata_list_and_detail_never_contain_body_or_preview(
    app: App,
) -> None:
    """AC：列表/详情只含元数据，无正文、预览或请求转储。"""
    repository = _RepositorySpy()
    repository.page = DecisionLogPage(
        items=(_metadata(),), total=1, page=1, page_size=50
    )
    repository.metadata = _metadata()

    async with _serve(
        app,
        repository=repository,
        health_provider=_HealthProvider(_health_snapshot()),
        audit=_AuditCollector(),
    ) as client:
        listing = await client.get(f"{API_PREFIX}/decisions", headers=_READ_HEADERS)
        detail = await client.get(
            f"{API_PREFIX}/decisions/{_DECISION_ID}", headers=_READ_HEADERS
        )

    assert listing.status_code == 200
    assert listing.headers["cache-control"] == NO_STORE
    payload = listing.json()
    assert payload["total"] == 1
    assert payload["page"] == 1
    assert payload["page_size"] == 50
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert item["decision_id"] == _DECISION_ID
    assert item["event"] == {
        "protocol": "onebot_v11",
        "account_id": "acct-1",
        "group_id": "group-1",
        "message_id": "msg-1",
    }
    assert item["result"]["requested_model"] == "jev-requested"
    assert item["result"]["actual_model"] == "jev-actual"
    assert item["result"]["usage"] == {"input_tokens": None, "output_tokens": None}
    assert item["result"]["reply_choice"]["value"] == "required"
    assert item["result"]["references"]["resolved_count"] == 1

    assert detail.status_code == 200
    assert detail.headers["cache-control"] == NO_STORE
    assert detail.json() == item

    for response_payload in (payload, detail.json()):
        keys = _all_keys(response_payload)
        assert "body" not in keys
        assert "preview" not in keys
        assert "state" not in keys
        assert "prompt" not in keys


async def test_metadata_list_forwards_every_filter_and_pagination(app: App) -> None:
    """AC：固定过滤与分页原样传给仓储（不在路由层重解释）。"""
    repository = _RepositorySpy()

    async with _serve(
        app,
        repository=repository,
        health_provider=_HealthProvider(_health_snapshot()),
        audit=_AuditCollector(),
    ) as client:
        response = await client.get(
            f"{API_PREFIX}/decisions",
            headers=_READ_HEADERS,
            query_string={
                "page": 2,
                "page_size": 10,
                "created_from": "2026-10-01T00:00:00+00:00",
                "created_before": "2026-10-02T00:00:00+00:00",
                "status": "filtered",
                "model": "jev-actual",
            },
        )

    assert response.status_code == 200
    (name, kwargs), *_rest = repository.calls
    assert name == "list_metadata"
    assert kwargs["page"] == 2
    assert kwargs["page_size"] == 10
    assert kwargs["status"] == "filtered"
    assert kwargs["model"] == "jev-actual"
    assert kwargs["created_from"] == datetime(2026, 10, 1, tzinfo=UTC)
    assert kwargs["created_before"] == datetime(2026, 10, 2, tzinfo=UTC)


async def test_missing_metadata_returns_404(app: App) -> None:
    """AC：不存在或已清理的日志返回 404（元数据与正文一致）。"""
    repository = _RepositorySpy()
    repository.metadata = None
    repository.body = None

    async with _serve(
        app,
        repository=repository,
        health_provider=_HealthProvider(_health_snapshot()),
        audit=_AuditCollector(),
    ) as client:
        detail = await client.get(
            f"{API_PREFIX}/decisions/{_DECISION_ID}", headers=_READ_HEADERS
        )
        body = await client.get(
            f"{API_PREFIX}/decisions/{_DECISION_ID}/body", headers=_BODY_HEADERS
        )

    for response in (detail, body):
        assert response.status_code == 404
        assert response.json() == {"detail": _FIXED_404_DETAIL}
        assert response.headers["cache-control"] == NO_STORE


# ------------------------------ 正文与审计 ------------------------------


async def test_body_read_is_audited_without_change_reason_header(app: App) -> None:
    """AC：正文读取审计起始+成功；无变更原因头；不含正文或原始 ID。"""
    audit = _AuditCollector()
    repository = _RepositorySpy()
    repository.body = DecisionLogBody(
        decision_id=_DECISION_ID, body={"state": _BODY_SECRET}
    )

    async with _serve(
        app,
        repository=repository,
        health_provider=_HealthProvider(_health_snapshot()),
        audit=audit,
    ) as client:
        response = await client.get(
            f"{API_PREFIX}/decisions/{_DECISION_ID}/body",
            headers={**_BODY_HEADERS, "X-Request-ID": "req-body-0001"},
        )

    assert response.status_code == 200
    assert response.headers["cache-control"] == NO_STORE
    assert response.json() == {
        "decision_id": _DECISION_ID,
        "body": {"state": _BODY_SECRET},
    }
    assert audit.outcomes == ["started", "succeeded"]
    _started, finished = audit.events
    for event in audit.events:
        assert event.operator_id == "decision-body-reader"
        assert event.action == BODY_READ_ACTION
        assert event.resource == DECISION_RESOURCE
        assert event.reason == BODY_READ_REASON
        assert event.request_id == "req-body-0001"
        assert event.target_hash == hash_management_target(str(UUID(_DECISION_ID)))
    assert finished.status_code == 200
    assert finished.metadata["body_present"] is True
    serialized = str([event.to_dict() for event in audit.events])
    assert _BODY_SECRET not in serialized
    assert _DECISION_ID not in serialized
    assert _BODY_TOKEN not in serialized


async def test_present_row_without_body_returns_null_body(app: App) -> None:
    """AC：存在但未捕获正文返回 200 且 body 为 null（与 404 区分）。"""
    audit = _AuditCollector()
    repository = _RepositorySpy()
    repository.body = DecisionLogBody(decision_id=_DECISION_ID, body=None)

    async with _serve(
        app,
        repository=repository,
        health_provider=_HealthProvider(_health_snapshot()),
        audit=audit,
    ) as client:
        response = await client.get(
            f"{API_PREFIX}/decisions/{_DECISION_ID}/body", headers=_BODY_HEADERS
        )

    assert response.status_code == 200
    assert response.json() == {"decision_id": _DECISION_ID, "body": None}
    assert audit.outcomes == ["started", "succeeded"]
    assert audit.events[-1].metadata["body_present"] is False


async def test_body_404_is_audited_as_failed(app: App) -> None:
    """AC：404 的正文尝试同样被审计为失败。"""
    audit = _AuditCollector()
    repository = _RepositorySpy()
    repository.body = None

    async with _serve(
        app,
        repository=repository,
        health_provider=_HealthProvider(_health_snapshot()),
        audit=audit,
    ) as client:
        response = await client.get(
            f"{API_PREFIX}/decisions/{_DECISION_ID}/body", headers=_BODY_HEADERS
        )

    assert response.status_code == 404
    assert audit.outcomes == ["started", "failed"]
    assert audit.events[-1].status_code == 404
    assert audit.events[-1].target_hash == hash_management_target(
        str(UUID(_DECISION_ID))
    )


async def test_body_storage_failure_returns_503_and_is_audited(app: App) -> None:
    """AC：正文存储不可用返回固定 503，并审计为失败。"""
    audit = _AuditCollector()
    repository = _RepositorySpy()
    repository.failures["get_body"] = DecisionLogStorageUnavailable()

    async with _serve(
        app,
        repository=repository,
        health_provider=_HealthProvider(_health_snapshot()),
        audit=audit,
    ) as client:
        response = await client.get(
            f"{API_PREFIX}/decisions/{_DECISION_ID}/body", headers=_BODY_HEADERS
        )

    assert response.status_code == 503
    assert response.json() == {"detail": _FIXED_503_DETAIL}
    assert response.headers["cache-control"] == NO_STORE
    assert audit.outcomes == ["started", "failed"]
    assert audit.events[-1].status_code == 503


async def test_audit_start_failure_denies_body_read(app: App) -> None:
    """AC：审计起始失败即返回 503，且完全不读取正文。"""
    audit = _AuditCollector(start_failure=RuntimeError("audit-sink-down"))
    repository = _RepositorySpy()
    repository.body = DecisionLogBody(decision_id=_DECISION_ID, body={"x": 1})

    async with _serve(
        app,
        repository=repository,
        health_provider=_HealthProvider(_health_snapshot()),
        audit=audit,
    ) as client:
        response = await client.get(
            f"{API_PREFIX}/decisions/{_DECISION_ID}/body", headers=_BODY_HEADERS
        )

    assert response.status_code == 503
    assert response.json() == {"detail": _FIXED_503_DETAIL}
    assert response.headers["cache-control"] == NO_STORE
    assert "audit-sink-down" not in response.text
    assert repository.calls == [], "审计未起始时不得读取正文"
    assert audit.events == []


# ------------------------------ 存储不可用 ------------------------------


async def test_storage_unavailability_never_returns_fake_empty_results(
    app: App,
) -> None:
    """AC：存储不可用返回 503，绝不伪造空列表或“不存在”。"""
    repository = _RepositorySpy()
    repository.failures["list_metadata"] = DecisionLogStorageUnavailable()
    repository.failures["get_metadata"] = DecisionLogStorageUnavailable()
    repository.failures["get_body"] = DecisionLogStorageUnavailable()

    async with _serve(
        app,
        repository=repository,
        health_provider=_HealthProvider(_health_snapshot()),
        audit=_AuditCollector(),
    ) as client:
        listing = await client.get(f"{API_PREFIX}/decisions", headers=_READ_HEADERS)
        detail = await client.get(
            f"{API_PREFIX}/decisions/{_DECISION_ID}", headers=_READ_HEADERS
        )
        body = await client.get(
            f"{API_PREFIX}/decisions/{_DECISION_ID}/body", headers=_BODY_HEADERS
        )

    for response in (listing, detail, body):
        assert response.status_code == 503
        assert response.json() == {"detail": _FIXED_503_DETAIL}
        assert response.headers["cache-control"] == NO_STORE


# ------------------------------ 请求校验 ------------------------------


async def test_malformed_identifier_and_query_values_return_fixed_422(
    app: App,
) -> None:
    """AC：非法 UUID/分页/时区/区间返回固定 422，且不回显输入。"""
    repository = _RepositorySpy()
    malformed_identifier = "not-a-uuid-4f0c"

    async with _serve(
        app,
        repository=repository,
        health_provider=_HealthProvider(_health_snapshot()),
        audit=_AuditCollector(),
    ) as client:
        detail = await client.get(
            f"{API_PREFIX}/decisions/{malformed_identifier}", headers=_READ_HEADERS
        )
        body = await client.get(
            f"{API_PREFIX}/decisions/{malformed_identifier}/body",
            headers=_BODY_HEADERS,
        )
        for params in (
            {"page": "0"},
            {"page": "abc"},
            {"page_size": "101"},
            {"page_size": "0"},
            {"status": "unknown-status"},
            {"created_from": "2026-10-01T00:00:00"},
            {
                "created_from": "2026-10-02T00:00:00+00:00",
                "created_before": "2026-10-01T00:00:00+00:00",
            },
        ):
            response = await client.get(
                f"{API_PREFIX}/decisions", headers=_READ_HEADERS, query_string=params
            )
            assert response.status_code == 422, params
            assert response.headers["cache-control"] == NO_STORE
            assert response.json() == {"detail": _FIXED_422_DETAIL}
            assert "traceback" not in response.text.lower()

        bad_request_id = await client.get(
            f"{API_PREFIX}/decisions/{_DECISION_ID}/body",
            headers={**_BODY_HEADERS, "X-Request-ID": "bad request id!"},
        )

    for response in (detail, body):
        assert response.status_code == 422
        assert response.json() == {"detail": _FIXED_IDENTIFIER_DETAIL}
        assert response.headers["cache-control"] == NO_STORE
        assert malformed_identifier not in response.text
    assert repository.calls == [], "非法标识不得进入仓储查询"
    assert bad_request_id.status_code == 422
    assert bad_request_id.headers["cache-control"] == NO_STORE


async def test_cache_control_no_store_covers_every_status_class(app: App) -> None:
    """AC：四个 GET 路由的每个状态类别都禁止缓存。"""
    repository = _RepositorySpy()
    repository.page = DecisionLogPage(
        items=(_metadata(),), total=1, page=1, page_size=50
    )
    repository.metadata = _metadata()
    repository.body = DecisionLogBody(decision_id=_DECISION_ID, body=None)

    health = _HealthProvider(_health_snapshot())
    async with _serve(
        app,
        repository=repository,
        health_provider=health,
        audit=_AuditCollector(),
    ) as client:
        observed: list[tuple[str, int]] = []
        requests: list[tuple[str, dict[str, str], dict[str, str]]] = [
            (f"{API_PREFIX}/health", {}, _READ_HEADERS),
            (f"{API_PREFIX}/decisions", {}, _READ_HEADERS),
            (f"{API_PREFIX}/decisions/{_DECISION_ID}", {}, _READ_HEADERS),
            (f"{API_PREFIX}/decisions/{_DECISION_ID}/body", {}, _BODY_HEADERS),
            (f"{API_PREFIX}/health", {}, {}),
            (f"{API_PREFIX}/decisions", {}, _OTHER_HEADERS),
            (f"{API_PREFIX}/decisions", {"page": "0"}, _READ_HEADERS),
        ]
        for path, query, headers in requests:
            response = await client.get(path, headers=headers, query_string=query)
            observed.append((path, response.status_code))
            assert response.headers["cache-control"] == NO_STORE, (path, headers)

        # 404 类别：仓储明确报告不存在。
        repository.metadata = None
        missing = await client.get(
            f"{API_PREFIX}/decisions/{uuid4()}", headers=_READ_HEADERS
        )
        observed.append(("detail-404", missing.status_code))
        assert missing.headers["cache-control"] == NO_STORE

        # 503 类别：健康快照来源失败。
        health.failure = RuntimeError("boom")
        unavailable = await client.get(f"{API_PREFIX}/health", headers=_READ_HEADERS)
        assert unavailable.headers["cache-control"] == NO_STORE
        observed.append((f"{API_PREFIX}/health", unavailable.status_code))

    codes = sorted({status_code for _path, status_code in observed})
    assert codes == [200, 401, 403, 404, 422, 503]
