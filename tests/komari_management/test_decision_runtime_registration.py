"""TSK-186 判定诊断管理面生产装配验收：真实 loader → 真实 registrar → 真实运行时。

被证明的生产链路（不是路由器工厂测试）：

``komari_management`` 真实模块的 ``_load_management_components`` →
``register_management_api_for_driver`` → 真实顶层
``komari_decision.register_decision_api`` → 进程内单例 ``DecisionRuntime`` 的
``get_health`` 与 ``log_repository`` → 真实 FastAPI ASGI 请求。

只有外部 I/O / 组合边界被替换（与 182 运行时装配契约一致）：

- 共享 ORM 会话端点：派生隔离库 + 记录会话与 SQL 的观测外壳（可在请求期内注入
  存储不可用）；
- 自有 Redis 客户端工厂：隔离 Redis DB15 + 观测外壳（只删本用例作用域键）；
- 官方 SDK transport：``ScriptedTransport``，零网络；
- 群准入：真实运行时实现，只替换其配置管理器（与 184 夹具同构）；
- 凭据来源：可变凭据列表（验证轮换/撤销不需要重挂载）；
- 审计持久化：内存记录器（可让起始事件失败）；
- 调度器实例：真实 APScheduler ``AsyncIOScheduler``。

期望值全部是手写字面量；断言只穿过 HTTP 状态码/响应体/响应头、真实审计事件、
真实 SQL 与真实数据库行。

关闭排空（在途清理被真实 PostgreSQL 阻塞 + 延迟会话归还 + 重复取消）已由
TSK-184 验收覆盖（``tests/komari_decision/test_jev_runtime.py`` 的
``test_shutdown_waits_for_real_prune_session_cleanup`` 等），本票复用该证据，
不重写关闭用例。
"""

from __future__ import annotations

import json
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
import redis.asyncio as redis_asyncio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import komari_bot.plugins.group_admission as admission_package
from komari_bot.decision.jev import (
    DecisionLogMetadata,
    DecisionResult,
)
from komari_bot.management.management_audit import (
    ManagementAuditEvent,
    hash_management_target,
)
from komari_bot.plugins.config_manager.manager import ConfigManager
from komari_bot.plugins.komari_decision import management_api as decision_management_api
from komari_bot.plugins.komari_decision import (
    register_decision_api as top_level_register_decision_api,
)
from komari_bot.plugins.komari_decision.config_schema import (
    KomariDecisionConfigSchema,
)
from komari_bot.plugins.komari_decision.prompt_schema import (
    DISPLAY_NAME,
    RESOURCE_ID,
)
from komari_bot.plugins.komari_decision.services import (
    jev_runtime as jev_runtime_module,
)
from komari_bot.plugins.komari_decision.services.jev_runtime import DecisionRuntime
from komari_bot.plugins.komari_management.api_runtime import (
    register_management_api_for_driver,
)
from tests.komari_decision.jev_test_support import (
    CONFIGURED_API_KEY,
    CONFIGURED_MODEL,
    TEST_REDIS_URL,
    ScriptedTransport,
    clean_test_scopes,
    delete_prompt,
    make_identity,
    make_request,
    read_dispatch,
    reset_decision_storage,
    skip_without_services,
    wait_until,
    write_config_row,
    write_prompt,
)
from tests.komari_decision.test_jev_runtime import _start_admission, decision_database
from tests.komari_management.test_plugin_integration import real_management_loader
from tests.pg_support import reset_shared_orm_engine

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

    from komari_bot.decision.jev import EventIdentity

__all__ = [
    # 直接复用既有验收夹具（同目录 / 判定目录），不复制整套运行时夹具。
    "decision_database",
    "real_management_loader",
]

_REAL_ADJUDICATE = admission_package.adjudicate
"""收集期捕获的真实群准入裁决符号。

``tests/komari_management/conftest.py`` 的 autouse 夹具会把包顶层
``adjudicate`` 换成恒 admit 替身（供既有管理路由测试使用）。本票要证明的是
**真实**运行时/引擎的准入裁决，因此在本模块夹具里恢复真实符号；替身的恢复仍由
该 conftest 的 ``monkeypatch`` 生命周期负责。
"""
assert _REAL_ADJUDICATE.__module__ == "komari_bot.plugins.group_admission"

API_PREFIX = "/api/v2/komari-decision"
NO_STORE = "no-store"
ORIGIN = "https://console.example.invalid"
BODY_COLUMN_SQL = "komari_decision_logs.body"
"""正文列只出现在受权正文查询里，用来证明「审计未起始就不查正文」。"""

READ_TOKEN = "decision-read-token-0001"
BODY_TOKEN = "decision-body-token-0001"
ADMIN_TOKEN = "decision-admin-token-0001"
SCENE_TOKEN = "decision-scene-token-0001"
ROTATED_TOKEN = "decision-read-token-0002"

READ_HEADERS = {"Authorization": f"Bearer {READ_TOKEN}"}
BODY_HEADERS = {"Authorization": f"Bearer {BODY_TOKEN}"}
ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
SCENE_HEADERS = {"Authorization": f"Bearer {SCENE_TOKEN}"}
ROTATED_HEADERS = {"Authorization": f"Bearer {ROTATED_TOKEN}"}

BODY_READ_ACTION = "decision_logs.read"
BODY_READ_REASON = "diagnostic_body_read"
DECISION_RESOURCE = "komari_decision"
FIXED_404_DETAIL = "判定日志不存在或已清理"
FIXED_QUERY_422_DETAIL = "判定日志查询参数无效"
FIXED_IDENTIFIER_422_DETAIL = "判定日志标识必须是 UUID"
FIXED_503_DETAIL = "判定日志存储暂不可用"


def _credential(
    credential_id: str,
    token: str,
    permissions: list[str],
    *,
    revoked_at: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "credential_id": credential_id,
        "token": token,
        "permissions": permissions,
    }
    if revoked_at is not None:
        payload["revoked_at"] = revoked_at
    return payload


def _initial_credentials() -> list[dict[str, object]]:
    """四类具名凭据：只读元数据 / 只读正文 / 通配 / 仅 scene。"""
    return [
        _credential("decision-reader", READ_TOKEN, ["decision:read"]),
        _credential("decision-body-reader", BODY_TOKEN, ["decision_logs:read"]),
        _credential("decision-admin", ADMIN_TOKEN, ["*"]),
        _credential("scene-operator", SCENE_TOKEN, ["scene:read"]),
    ]


class _FakeLogger:
    """最小 logger 替身：只记录注册期文本。"""

    def __init__(self) -> None:
        self.info_messages: list[str] = []
        self.warning_messages: list[str] = []

    def info(self, message: str, *args: object) -> None:
        self.info_messages.append(message % args if args else message)

    def warning(self, message: str, *args: object) -> None:
        self.warning_messages.append(message % args if args else message)


# ------------------------------ 请求作用域归属 ------------------------------


_HTTP_SCOPE: ContextVar[bool] = ContextVar("tsk186_http_scope", default=False)
"""真实 HTTP 请求作用域标记。

动态凭据、SQL 与 Redis 观测都用它把「请求自身产生的 I/O」和引擎/调度器的合法
后台归属工作（租约轮询、周期刷新、清理）分开，避免易抖的全局计数器。
"""


class _RequestScopeMiddleware:
    """把每个 HTTP 请求标记成请求作用域（纯 ASGI，不改业务语义）。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = _HTTP_SCOPE.set(True)
        try:
            await self.app(scope, receive, send)
        finally:
            _HTTP_SCOPE.reset(token)


@dataclass
class _IoProbe:
    """真实 SQL / 会话 / Redis 调用的观测面（不改变被观测对象的行为）。"""

    statements: list[tuple[bool, str]] = field(default_factory=list)
    sessions: list[bool] = field(default_factory=list)
    redis_operations: list[tuple[bool, str]] = field(default_factory=list)
    outage: bool = False
    """为真时只让**请求作用域**内的会话建立失败（外部存储端点故障）。"""

    def record_statement(
        self,
        _conn: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,  # noqa: FBT001 - SQLAlchemy 监听器签名固定为位置参数
    ) -> None:
        self.statements.append((_HTTP_SCOPE.get(), statement))

    def record_redis(self, operation: str) -> None:
        self.redis_operations.append((_HTTP_SCOPE.get(), operation))

    def reset(self) -> None:
        self.statements.clear()
        self.sessions.clear()
        self.redis_operations.clear()

    def request_statements(self) -> list[str]:
        return [statement for in_scope, statement in self.statements if in_scope]

    def request_body_statements(self) -> list[str]:
        return [
            statement
            for statement in self.request_statements()
            if BODY_COLUMN_SQL in statement
        ]

    def request_sessions(self) -> int:
        return sum(1 for in_scope in self.sessions if in_scope)

    def request_redis_calls(self) -> list[str]:
        return [operation for in_scope, operation in self.redis_operations if in_scope]


class _ProbedSessionEndpoint:
    """共享 ORM 会话端点替身：记录会话归属，可按请求注入连接失败。"""

    def __init__(
        self,
        factory: Any,
        probe: _IoProbe,
    ) -> None:
        self._factory = factory
        self._probe = probe

    def __call__(self) -> AsyncSession:
        in_scope = _HTTP_SCOPE.get()
        self._probe.sessions.append(in_scope)
        if self._probe.outage and in_scope:
            message = "synthetic session endpoint outage"
            raise ConnectionError(message)
        return cast("AsyncSession", self._factory())


class _ProbedRedis:
    """真实 Redis 客户端的观测外壳：只记录调用名并按请求作用域归属。"""

    def __init__(self, client: Any, probe: _IoProbe) -> None:
        self._client = client
        self._probe = probe

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._client, name)
        if not callable(attribute):
            return attribute

        def _call(*args: Any, **kwargs: Any) -> Any:
            self._probe.record_redis(name)
            return attribute(*args, **kwargs)

        return _call


# ------------------------------ 审计与凭据 ------------------------------


@dataclass
class _AuditSink:
    """审计持久化边界替身：真实 span 语义不变，可让起始事件失败。"""

    events: list[ManagementAuditEvent] = field(default_factory=list)
    fail_on_started: bool = False

    async def __call__(self, audit_event: ManagementAuditEvent) -> None:
        if self.fail_on_started and audit_event.outcome == "started":
            message = "synthetic audit sink outage"
            raise RuntimeError(message)
        self.events.append(audit_event)

    @property
    def outcomes(self) -> list[str]:
        return [audit_event.outcome for audit_event in self.events]


@dataclass
class _CredentialSource:
    """动态凭据来源：每次鉴权读取当前列表（轮换/撤销无需重挂载）。"""

    credentials: list[dict[str, object]]

    def __call__(self) -> list[dict[str, object]]:
        return list(self.credentials)


# ------------------------------ 生产装配夹具 ------------------------------


@dataclass
class _ManagementEnv:
    app: FastAPI
    client: httpx.AsyncClient
    runtime: DecisionRuntime
    manager: ConfigManager
    transport: ScriptedTransport
    probe: _IoProbe
    audit: _AuditSink
    credentials: _CredentialSource
    engine: AsyncEngine
    mount: Any
    components: Any
    last_identity: EventIdentity | None = None

    async def evaluate(self) -> Any:
        """经真实引擎生成一条判定（并落一条真实诊断日志）。"""
        engine = self.runtime.engine
        assert engine is not None
        identity = make_identity(
            protocol="onebot_v11",
            account_id="100000001",
            group_id="200000001",
            message_id=f"msg-{uuid4().hex[:10]}",
        )
        self.last_identity = identity
        return await engine.evaluate(
            make_request(identity=identity, text="今晚谁值班？")
        )

    async def record_diagnostic(self, *, created_at: datetime) -> str:
        """经真实仓储写入一条额外诊断行（用于保留期/清理观测）。"""
        normalized = str(uuid4())
        identity = make_identity(
            protocol="onebot_v11",
            account_id="100000002",
            group_id="200000002",
            message_id=f"msg-{uuid4().hex[:10]}",
        )
        await self.runtime.log_repository.record(
            _diagnostic_metadata(
                decision_id=normalized, identity=identity, created_at=created_at
            ),
            body={"state": {"note": "retention-probe"}, "questions": {}},
        )
        return normalized


def _diagnostic_metadata(
    *, decision_id: str, identity: EventIdentity, created_at: datetime
) -> DecisionLogMetadata:
    return DecisionLogMetadata(
        decision_id=decision_id,
        event=identity,
        created_at=created_at,
        finished_at=created_at,
        result=DecisionResult(
            decision_id=decision_id,
            status="success",
            reason_code="classified",
        ),
    )


@pytest.fixture
def orm_plugin_loaded() -> Any:
    """先于 loader 夹具加载 ORM 插件（loader 夹具会替换 ``get_driver``）。"""
    from nonebot import require as nonebot_require

    nonebot_require("nonebot_plugin_orm")
    import nonebot_plugin_orm

    return nonebot_plugin_orm


@pytest.fixture
async def management_env(
    orm_plugin_loaded: Any,
    decision_database: str,
    real_management_loader: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[_ManagementEnv]:
    """真实 loader + 真实 registrar + 真实运行时 + 真实 HTTP。"""
    skip_without_services()
    orm_module = orm_plugin_loaded

    from komari_bot.config.prompt_storage import PromptTemplateLoader

    await reset_shared_orm_engine()
    engine = create_async_engine(
        decision_database,
        connect_args={
            "server_settings": {"application_name": f"tsk186-{uuid4().hex[:8]}"}
        },
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    def _shared_get_session(*_args: object, **_kwargs: object) -> AsyncSession:
        return cast("AsyncSession", session_factory())

    # 共享 ORM 会话端点（config/Prompt 存储）指向本次派生库。
    monkeypatch.setattr(orm_module, "get_session", _shared_get_session)
    # 恢复真实准入裁决（目录级 autouse 替身只服务既有管理路由测试）。
    monkeypatch.setattr(
        admission_package, "adjudicate", _REAL_ADJUDICATE, raising=False
    )
    assert admission_package.adjudicate is _REAL_ADJUDICATE
    admission_runtime = await _start_admission(monkeypatch)
    await reset_decision_storage(engine)
    await write_config_row(engine, overrides={"plugin_enable": True})
    await write_prompt()

    probe = _IoProbe()
    event.listen(engine.sync_engine, "before_cursor_execute", probe.record_statement)
    redis_client = redis_asyncio.Redis.from_url(TEST_REDIS_URL)
    observed_redis = _ProbedRedis(redis_client, probe)
    transport = ScriptedTransport()
    manager = ConfigManager("komari_decision", KomariDecisionConfigSchema)

    def _redis_factory() -> redis_asyncio.Redis:
        """真实 Redis 的观测外壳（所有权随后转移给真实引擎）。"""
        return cast("redis_asyncio.Redis", observed_redis)

    runtime = DecisionRuntime(
        config_manager=manager,
        session_factory=_ProbedSessionEndpoint(session_factory, probe),  # type: ignore[arg-type]
        redis_factory=_redis_factory,
        prompt_loader=PromptTemplateLoader(
            resource_id=RESOURCE_ID,
            display_name=DISPLAY_NAME,
            log_prefix="[tsk186]",
        ),
        transport_factory=lambda: transport,
        bot_resolver=lambda _account_id: None,
    )
    # 生产单例选择：真实 get_decision_runtime() 返回本真实运行时。
    monkeypatch.setattr(jev_runtime_module, "_runtime", runtime)

    audit = _AuditSink()
    monkeypatch.setattr(decision_management_api, "record_management_audit_event", audit)
    credentials = _CredentialSource(_initial_credentials())

    # 生产 loader（真实 komari_management 模块文件）产出的真实组件。
    components = real_management_loader.load_components()
    app = FastAPI(docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
    app.add_middleware(_RequestScopeMiddleware)
    logger = _FakeLogger()

    def _mount() -> bool:
        return register_management_api_for_driver(
            driver=SimpleNamespace(type="fastapi", server_app=app),
            config=SimpleNamespace(
                plugin_enable=True,
                api_credentials=credentials(),
                api_allowed_origins=[ORIGIN],
            ),
            component_loader=lambda: components,
            logger=logger,
            api_token_getter=credentials,
        )

    assert _mount() is True
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://management.test",
    )
    env = _ManagementEnv(
        app=app,
        client=client,
        runtime=runtime,
        manager=manager,
        transport=transport,
        probe=probe,
        audit=audit,
        credentials=credentials,
        engine=engine,
        mount=_mount,
        components=components,
    )
    try:
        yield env
    finally:
        with suppress(BaseException):
            await client.aclose()
        with suppress(BaseException):
            await runtime.close()
        with suppress(Exception):
            await admission_runtime.close()
        with suppress(Exception):
            await clean_test_scopes(redis_client)
        with suppress(Exception):
            await redis_client.aclose()
        with suppress(Exception):
            event.remove(
                engine.sync_engine, "before_cursor_execute", probe.record_statement
            )
        await delete_prompt()
        await engine.dispose()
        await reset_shared_orm_engine()


def _decision_routes(app: FastAPI) -> list[Any]:
    """展开 FastAPI 0.139 的 ``_IncludedRouter`` 后取出判定路由对象。"""
    pending: list[Any] = list(app.routes)
    found: list[Any] = []
    while pending:
        route = pending.pop()
        path = str(getattr(route, "path", "") or "")
        if path == API_PREFIX or path.startswith(f"{API_PREFIX}/"):
            found.append(route)
            continue
        nested = getattr(route, "routes", None)
        if nested is None:
            original = getattr(route, "original_router", None)
            nested = getattr(original, "routes", None)
        if nested:
            pending.extend(nested)
    return found


def _cors_layer_count(app: FastAPI) -> int:
    return sum(1 for layer in app.user_middleware if layer.cls is CORSMiddleware)


def _all_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(str(key))
            keys |= _all_keys(item)
    elif isinstance(value, list):
        for item in value:
            keys |= _all_keys(item)
    return keys


# ------------------------------ 1. 生产 loader 与挂载 ------------------------------


async def test_production_loader_mounts_exactly_four_decision_routes_once(
    management_env: _ManagementEnv,
) -> None:
    """AC1：真实 loader 携带真实 registrar；重复挂载不复制路由或 CORS。"""
    env = management_env
    assert env.components.register_decision_api is top_level_register_decision_api, (
        "生产 loader 必须携带真实顶层 register_decision_api"
    )

    expected_paths = {
        f"{API_PREFIX}/health",
        f"{API_PREFIX}/decisions",
        f"{API_PREFIX}/decisions/{{decision_id}}",
        f"{API_PREFIX}/decisions/{{decision_id}}/body",
    }
    mounted = _decision_routes(env.app)
    assert {route.path for route in mounted} == expected_paths
    assert len(mounted) == 4, "四个判定端点各一份，没有重复"
    for route in mounted:
        assert set(route.methods) <= {"GET", "HEAD"}, route.path
    before = list(env.app.routes)
    assert _cors_layer_count(env.app) == 1

    await env.runtime.startup()
    health = await env.client.get(f"{API_PREFIX}/health", headers=READ_HEADERS)
    assert health.status_code == 200
    assert health.json()["runtime_status"] == "ready"

    # 重复挂载：路由与 CORS 都没有第二份。
    assert env.mount() is True
    assert list(env.app.routes) == before
    repeated_routes = _decision_routes(env.app)
    assert len(repeated_routes) == 4
    assert [route.path for route in repeated_routes] == [
        route.path for route in mounted
    ]
    assert _cors_layer_count(env.app) == 1
    repeated = await env.client.get(f"{API_PREFIX}/health", headers=READ_HEADERS)
    assert repeated.status_code == 200


# ------------------------------ 2. 被动健康 ------------------------------


async def test_health_stays_passive_and_truthful_across_runtime_states(
    management_env: _ManagementEnv,
) -> None:
    """AC2：ready/disabled/failed 都可查询且被动；观测来自真实引擎。"""
    env = management_env
    await env.runtime.startup()
    result = await env.evaluate()
    assert result.status == "success"
    assert len(env.transport.requests) == 1

    env.probe.reset()
    ready = await env.client.get(f"{API_PREFIX}/health", headers=READ_HEADERS)
    assert ready.status_code == 200
    assert ready.headers["cache-control"] == NO_STORE
    payload = ready.json()
    assert payload["runtime_status"] == "ready"
    assert payload["reason_code"] == "ready"
    assert payload["last_request_status"] == "success"
    assert payload["consecutive_failures"] == 0
    # 真实请求观测存在时如实报告 0（不是伪造的 null）。
    assert payload["in_flight_count"] == 0
    assert payload["queued_count"] == 0
    assert payload["last_success_at"] is not None
    assert payload["last_failure_at"] is None
    # 健康查询零请求期 I/O：不建会话、不查日志表、不访问 Redis。
    assert env.probe.request_sessions() == 0
    assert env.probe.request_statements() == []
    assert env.probe.request_redis_calls() == []

    # 失败态：真实配置校验失败，请求观测原样保留（不伪造成功）。
    await env.manager.update_field_async("jev_api_key", "")
    await env.runtime.refresh()
    env.probe.reset()
    failed = await env.client.get(f"{API_PREFIX}/health", headers=READ_HEADERS)
    assert failed.status_code == 200
    failed_payload = failed.json()
    assert failed_payload["runtime_status"] == "failed"
    assert failed_payload["reason_code"] == "configuration_invalid"
    assert failed_payload["last_request_status"] == "success"
    assert failed_payload["last_success_at"] == payload["last_success_at"]
    assert env.probe.request_statements() == []
    assert env.probe.request_redis_calls() == []

    # 显式关闭态：路由仍在，健康如实报告 disabled。
    await env.manager.update_field_async(
        "jev_api_key",
        CONFIGURED_API_KEY,
    )
    await env.manager.update_field_async(
        "plugin_enable",
        False,  # noqa: FBT003 - 字段值本身是布尔
    )
    await env.runtime.refresh()
    env.probe.reset()
    disabled = await env.client.get(f"{API_PREFIX}/health", headers=READ_HEADERS)
    assert disabled.status_code == 200
    assert disabled.json()["runtime_status"] == "disabled"
    assert disabled.json()["reason_code"] == "disabled"
    assert env.probe.request_statements() == []
    # 正控制：同一归属标记下，真实查询请求确实会产生请求期会话与 SQL。
    env.probe.reset()
    still_there = await env.client.get(f"{API_PREFIX}/decisions", headers=READ_HEADERS)
    assert still_there.status_code == 200
    assert still_there.headers["cache-control"] == NO_STORE
    assert env.probe.request_sessions() >= 1, "请求期归属标记必须真的生效"
    assert env.probe.request_statements(), "请求期归属标记必须真的生效"
    assert len(env.transport.requests) == 1, "健康查询不得产生新的模型请求"


async def test_uninitialized_retention_is_unavailable_without_invented_default(
    management_env: _ManagementEnv,
) -> None:
    """配置快照尚未加载时日志查询返回 503，不使用虚构保留期。"""
    env = management_env
    env.probe.reset()
    health = await env.client.get(f"{API_PREFIX}/health", headers=READ_HEADERS)
    assert health.status_code == 200
    assert health.json()["runtime_status"] == "failed"
    assert health.json()["last_request_status"] == "unknown"
    assert health.json()["last_success_at"] is None
    assert health.json()["last_failure_at"] is None
    assert env.probe.request_sessions() == 0
    assert env.probe.request_statements() == []
    assert env.probe.request_redis_calls() == []

    missing_id = str(uuid4())
    for suffix, headers in (
        ("/decisions", READ_HEADERS),
        (f"/decisions/{missing_id}", READ_HEADERS),
        (f"/decisions/{missing_id}/body", BODY_HEADERS),
    ):
        unavailable = await env.client.get(f"{API_PREFIX}{suffix}", headers=headers)
        assert unavailable.status_code == 503
        assert unavailable.json() == {"detail": FIXED_503_DETAIL}
        assert unavailable.headers["cache-control"] == NO_STORE
    assert env.audit.outcomes == ["started", "failed"]

    # 同一数据库和路由在真实启动加载配置后恢复，无需伪造快照。
    await env.runtime.startup()
    available = await env.client.get(f"{API_PREFIX}/decisions", headers=READ_HEADERS)
    assert available.status_code == 200
    assert available.json()["total"] == 0
    assert env.transport.requests == []


# ------------------------------ 3. 真实引擎日志可读 ------------------------------


async def test_real_engine_log_is_readable_through_metadata_and_body(
    management_env: _ManagementEnv,
) -> None:
    """AC3：真实引擎写入的日志经元数据/正文端点可见，权限互相独立。"""
    env = management_env
    await env.runtime.startup()
    result = await env.evaluate()
    decision_id = result.decision_id
    normalized = str(UUID(decision_id))

    listing = await env.client.get(f"{API_PREFIX}/decisions", headers=READ_HEADERS)
    assert listing.status_code == 200
    assert listing.headers["cache-control"] == NO_STORE
    page = listing.json()
    assert page["total"] == 1
    assert page["page"] == 1
    assert page["page_size"] == 50
    item = page["items"][0]
    assert item["decision_id"] == normalized
    assert item["event"]["protocol"] == "onebot_v11"
    assert item["event"]["account_id"] == "100000001"
    assert item["event"]["group_id"] == "200000001"
    assert item["result"]["status"] == "success"
    assert item["result"]["route"] == "chat"
    assert item["result"]["requested_model"] == CONFIGURED_MODEL
    assert item["result"]["actual_model"] == CONFIGURED_MODEL
    assert item["result"]["usage"] == {"input_tokens": 128, "output_tokens": 9}
    assert item["result"]["config_revision"] == 1
    assert item["result"]["prompt_revision"] == 1
    keys = _all_keys(page)
    assert "body" not in keys
    assert "preview" not in keys
    assert "prompt" not in keys

    detail = await env.client.get(
        f"{API_PREFIX}/decisions/{normalized}", headers=READ_HEADERS
    )
    assert detail.status_code == 200
    assert detail.json() == item

    # 权限互相独立：只读正文的凭据读不到元数据；只读元数据读不到正文。
    body_denied = await env.client.get(
        f"{API_PREFIX}/decisions/{normalized}/body", headers=READ_HEADERS
    )
    assert body_denied.status_code == 403
    assert body_denied.headers["cache-control"] == NO_STORE
    metadata_denied = await env.client.get(
        f"{API_PREFIX}/decisions", headers=BODY_HEADERS
    )
    assert metadata_denied.status_code == 403

    body = await env.client.get(
        f"{API_PREFIX}/decisions/{normalized}/body", headers=BODY_HEADERS
    )
    assert body.status_code == 200
    assert body.headers["cache-control"] == NO_STORE
    payload = body.json()
    assert payload["decision_id"] == normalized
    captured = payload["body"]
    assert set(captured) == {"state", "questions", "response", "response_status"}
    assert captured["response_status"] == 200
    assert sorted(captured["questions"]) == [
        "reply_eligibility",
        "retention_purpose",
    ]
    assert captured["response"]["answers"]["reply_eligibility"]["choice"] == "required"
    rendered = json.dumps(captured, ensure_ascii=False)
    assert CONFIGURED_API_KEY not in rendered

    admin_body = await env.client.get(
        f"{API_PREFIX}/decisions/{normalized}/body", headers=ADMIN_HEADERS
    )
    assert admin_body.status_code == 200
    assert admin_body.json() == payload


# ------------------------------ 4. 凭据矩阵 ------------------------------


async def test_credentials_rotate_and_revoke_without_remounting(
    management_env: _ManagementEnv,
) -> None:
    """AC4：错误/缺失/scene-only/轮换/撤销都在既有动态凭据来源上生效。"""
    env = management_env
    await env.runtime.startup()
    routes = (f"{API_PREFIX}/health", f"{API_PREFIX}/decisions")
    before_routes = list(env.app.routes)

    for path in routes:
        anonymous = await env.client.get(path)
        assert anonymous.status_code == 401, path
        assert anonymous.headers["cache-control"] == NO_STORE
        forged = await env.client.get(
            path, headers={"Authorization": "Bearer not-a-real-decision-token"}
        )
        assert forged.status_code == 401, path
        # scene:read 凭据有效（不是 401）但没有判定权限（不是 200）。
        scene = await env.client.get(path, headers=SCENE_HEADERS)
        assert scene.status_code == 403, path
        assert scene.headers["cache-control"] == NO_STORE

    # 轮换：旧 token 立即失效，新 token 立即生效，且未重挂载。
    env.credentials.credentials = [
        _credential("decision-reader", ROTATED_TOKEN, ["decision:read"]),
        _credential("decision-body-reader", BODY_TOKEN, ["decision_logs:read"]),
        _credential("decision-admin", ADMIN_TOKEN, ["*"]),
        _credential("scene-operator", SCENE_TOKEN, ["scene:read"]),
    ]
    stale = await env.client.get(f"{API_PREFIX}/decisions", headers=READ_HEADERS)
    assert stale.status_code == 401
    rotated = await env.client.get(f"{API_PREFIX}/decisions", headers=ROTATED_HEADERS)
    assert rotated.status_code == 200

    # 只撤销权限时凭据仍有效，但判定读取立即返回 403。
    env.credentials.credentials = [
        _credential("decision-reader", ROTATED_TOKEN, ["scene:read"]),
    ]
    permission_denied = await env.client.get(
        f"{API_PREFIX}/decisions", headers=ROTATED_HEADERS
    )
    assert permission_denied.status_code == 403
    assert permission_denied.headers["cache-control"] == NO_STORE

    # 撤销：同一 token 在撤销时间之后失效（不重挂载、不新建路由）。
    env.credentials.credentials = [
        _credential(
            "decision-reader",
            ROTATED_TOKEN,
            ["decision:read"],
            revoked_at="2020-01-01T00:00:00+00:00",
        ),
        _credential("decision-body-reader", BODY_TOKEN, ["decision_logs:read"]),
        _credential("decision-admin", ADMIN_TOKEN, ["*"]),
    ]
    revoked = await env.client.get(f"{API_PREFIX}/decisions", headers=ROTATED_HEADERS)
    assert revoked.status_code == 401
    assert revoked.headers["cache-control"] == NO_STORE
    assert list(env.app.routes) == before_routes


# ------------------------------ 5. 审计 ------------------------------


async def test_audit_records_allowed_success_not_found_and_unavailable_attempts(
    management_env: _ManagementEnv,
) -> None:
    """AC5：正文读取的成功/404/503 都写真实审计；目标哈希不含原始 ID。"""
    env = management_env
    await env.runtime.startup()
    result = await env.evaluate()
    normalized = str(UUID(result.decision_id))
    request_id = "tsk186-body-0001"

    allowed = await env.client.get(
        f"{API_PREFIX}/decisions/{normalized}/body",
        headers={**BODY_HEADERS, "X-Request-ID": request_id},
    )
    assert allowed.status_code == 200
    assert env.audit.outcomes == ["started", "succeeded"]
    started, finished = env.audit.events
    for audit_event in env.audit.events:
        assert audit_event.operator_id == "decision-body-reader"
        assert audit_event.action == BODY_READ_ACTION
        assert audit_event.resource == DECISION_RESOURCE
        assert audit_event.reason == BODY_READ_REASON
        assert audit_event.request_id == request_id
        assert audit_event.target_hash == hash_management_target(normalized)
    assert started.status_code is None, "起始审计事件没有终态状态码"
    assert finished.status_code == 200
    assert finished.metadata["body_present"] is True
    rendered = str([audit_event.to_dict() for audit_event in env.audit.events])
    assert BODY_TOKEN not in rendered
    assert normalized not in rendered

    missing_id = str(uuid4())
    env.audit.events.clear()
    missing = await env.client.get(
        f"{API_PREFIX}/decisions/{missing_id}/body", headers=BODY_HEADERS
    )
    assert missing.status_code == 404
    assert env.audit.outcomes == ["started", "failed"]
    assert env.audit.events[-1].status_code == 404
    assert env.audit.events[-1].target_hash == hash_management_target(missing_id)

    # 真实存储端点不可用：503 且同样留下失败审计。
    env.audit.events.clear()
    env.probe.outage = True
    try:
        unavailable = await env.client.get(
            f"{API_PREFIX}/decisions/{normalized}/body", headers=BODY_HEADERS
        )
    finally:
        env.probe.outage = False
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": FIXED_503_DETAIL}
    assert unavailable.headers["cache-control"] == NO_STORE
    assert env.audit.outcomes == ["started", "failed"]
    assert env.audit.events[-1].status_code == 503


# ------------------------------ 6. 审计起始失败 ------------------------------


async def test_audit_begin_failure_blocks_the_body_query(
    management_env: _ManagementEnv,
) -> None:
    """AC6：审计起始失败返回 503，且不产生任何正文查询（真实 SQL 证据）。"""
    env = management_env
    await env.runtime.startup()
    result = await env.evaluate()
    normalized = str(UUID(result.decision_id))

    # 先建立正证据：成功读取确实会执行正文列查询。
    env.probe.reset()
    allowed = await env.client.get(
        f"{API_PREFIX}/decisions/{normalized}/body", headers=BODY_HEADERS
    )
    assert allowed.status_code == 200
    assert env.probe.request_body_statements(), "受权正文读取必须真的查询正文列"

    env.audit.events.clear()
    env.audit.fail_on_started = True
    env.probe.reset()
    denied = await env.client.get(
        f"{API_PREFIX}/decisions/{normalized}/body", headers=BODY_HEADERS
    )
    assert denied.status_code == 503
    assert denied.json() == {"detail": FIXED_503_DETAIL}
    assert denied.headers["cache-control"] == NO_STORE
    assert "synthetic audit sink outage" not in denied.text
    assert env.probe.request_body_statements() == []
    assert env.probe.request_sessions() == 0
    assert env.audit.events == []
    env.audit.fail_on_started = False

    # 正文行仍然存在：拒绝来自审计边界，而不是日志被删除。
    readable = await env.client.get(
        f"{API_PREFIX}/decisions/{normalized}/body", headers=BODY_HEADERS
    )
    assert readable.status_code == 200


# ------------------------------ 7. 保留期 ------------------------------


async def test_current_retention_changes_read_visibility_without_remounting(
    management_env: _ManagementEnv,
) -> None:
    """AC7：当前保留期改变可见性；过期与不存在都是 404。"""
    env = management_env
    await env.runtime.startup()
    await env.evaluate()
    before_routes = list(env.app.routes)

    old_row = await env.record_diagnostic(
        created_at=datetime.now(UTC) - timedelta(days=10)
    )
    fresh_row = await env.record_diagnostic(
        created_at=datetime.now(UTC) - timedelta(days=1)
    )

    hidden = await env.client.get(
        f"{API_PREFIX}/decisions/{old_row}", headers=READ_HEADERS
    )
    assert hidden.status_code == 404
    assert hidden.json() == {"detail": FIXED_404_DETAIL}
    assert hidden.headers["cache-control"] == NO_STORE
    visible = await env.client.get(
        f"{API_PREFIX}/decisions/{fresh_row}", headers=READ_HEADERS
    )
    assert visible.status_code == 200

    # 保留期放宽到 30 天：同一行立刻可见，没有重挂载、没有重启。
    await env.manager.update_field_async("decision_log_retention_days", 30)
    widened = await env.client.get(
        f"{API_PREFIX}/decisions/{old_row}", headers=READ_HEADERS
    )
    assert widened.status_code == 200
    assert widened.json()["decision_id"] == old_row
    widened_body = await env.client.get(
        f"{API_PREFIX}/decisions/{old_row}/body", headers=BODY_HEADERS
    )
    assert widened_body.status_code == 200
    assert widened_body.json()["body"]["state"] == {"note": "retention-probe"}

    # 收窄回 7 天：重新变为 404；不存在的标识同样是 404。
    await env.manager.update_field_async("decision_log_retention_days", 7)
    narrowed = await env.client.get(
        f"{API_PREFIX}/decisions/{old_row}", headers=READ_HEADERS
    )
    assert narrowed.status_code == 404
    narrowing_body = await env.client.get(
        f"{API_PREFIX}/decisions/{old_row}/body", headers=BODY_HEADERS
    )
    assert narrowing_body.status_code == 404
    unknown = await env.client.get(
        f"{API_PREFIX}/decisions/{uuid4()}", headers=READ_HEADERS
    )
    assert unknown.status_code == 404
    assert list(env.app.routes) == before_routes


# ------------------------------ 8. 真实调度器清理 ------------------------------


async def test_real_scheduler_prune_removes_expired_diagnostics_while_disabled(
    management_env: _ManagementEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC8：真实 APScheduler 安装的清理 job 在判定关闭时仍执行。"""
    from apscheduler.events import EVENT_JOB_EXECUTED
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from komari_bot.plugins.komari_decision.services import jev_lifecycle

    env = management_env
    await env.runtime.startup()
    result = await env.evaluate()
    preserved_id = str(UUID(result.decision_id))
    identity = env.last_identity
    assert identity is not None

    expired_row = await env.record_diagnostic(
        created_at=datetime.now(UTC) - timedelta(days=10)
    )

    await env.manager.update_field_async(
        "plugin_enable",
        False,  # noqa: FBT003 - 字段值本身是布尔
    )
    await env.runtime.refresh()
    assert env.runtime.get_health().runtime_status == "disabled"

    # 关闭本身会定案在途分发；清理前后的基线取在关闭结算之后。
    durable_before = await read_dispatch(env.engine, identity)
    assert durable_before is not None
    counts_before = await _table_counts(env.engine)

    scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    executed: list[str] = []
    scheduler.add_listener(
        lambda event_record: executed.append(event_record.job_id),
        EVENT_JOB_EXECUTED,
    )
    monkeypatch.setattr(jev_lifecycle, "scheduler", scheduler)
    scheduler.start(paused=True)
    try:
        await jev_lifecycle._startup()
        assert {job.id for job in scheduler.get_jobs()} == {
            "komari_decision_refresh",
            "komari_decision_prune",
        }
        prune_job = scheduler.get_job("komari_decision_prune")
        assert prune_job is not None
        prune_job.modify(next_run_time=datetime.now(UTC))
        scheduler.resume()
        await wait_until(lambda: "komari_decision_prune" in executed, within_seconds=10)

        # EVENT_JOB_EXECUTED 在 job 协程返回后才派发：此时清理已提交。
        assert await _log_row_exists(env.engine, expired_row) is False, (
            "真实调度器的清理 job 必须删除过期诊断行"
        )
        assert await _log_row_exists(env.engine, preserved_id)

        # 清理只删除过期诊断行：持久身份/去重/所有权/调度/群序事实不变。
        durable_after = await read_dispatch(env.engine, identity)
        assert durable_after == durable_before
        assert await _table_counts(env.engine) == counts_before

        # 判定关闭期间管理面仍可查询（路由未被关闭/失败移除）。
        health = await env.client.get(f"{API_PREFIX}/health", headers=READ_HEADERS)
        assert health.status_code == 200
        assert health.json()["runtime_status"] == "disabled"
        pruned = await env.client.get(
            f"{API_PREFIX}/decisions/{expired_row}", headers=READ_HEADERS
        )
        assert pruned.status_code == 404
        kept = await env.client.get(
            f"{API_PREFIX}/decisions/{preserved_id}", headers=READ_HEADERS
        )
        assert kept.status_code == 200
    finally:
        with suppress(Exception):
            await jev_lifecycle._shutdown()
        scheduler.shutdown(wait=True)


async def _log_row_exists(engine: AsyncEngine, decision_id: str) -> bool:
    """诊断行是否仍物理存在（真实 SQL，不经管理面）。"""
    async with engine.connect() as connection:
        found = await connection.scalar(
            text("SELECT count(*) FROM komari_decision_logs WHERE id = :row"),
            {"row": decision_id},
        )
    return int(found or 0) == 1


async def _table_counts(engine: AsyncEngine) -> dict[str, int]:
    """判定自有持久事实的行数（清理不得触碰这些表）。"""
    tables = (
        "komari_decision_dispatches",
        "komari_decision_processes",
        "komari_decision_scheduler",
        "komari_decision_groups",
        "komari_decision_config",
    )
    counts: dict[str, int] = {}
    async with engine.connect() as connection:
        for table in tables:
            counts[table] = int(
                await connection.scalar(text(f"SELECT count(*) FROM {table}")) or 0
            )
    return counts


# ------------------------------ 9. 缓存与错误矩阵 ------------------------------


async def test_no_store_and_safe_error_matrix_through_production_mount(
    management_env: _ManagementEnv,
) -> None:
    """AC9：四个路由的 200/401/403/404/422/503 都不缓存、不泄露内部细节。"""
    env = management_env
    await env.runtime.startup()
    result = await env.evaluate()
    normalized = str(UUID(result.decision_id))
    observed: list[int] = []

    requests: list[tuple[str, dict[str, str], dict[str, str]]] = [
        (f"{API_PREFIX}/health", {}, READ_HEADERS),
        (f"{API_PREFIX}/decisions", {}, READ_HEADERS),
        (f"{API_PREFIX}/decisions/{normalized}", {}, READ_HEADERS),
        (f"{API_PREFIX}/decisions/{normalized}/body", {}, BODY_HEADERS),
        (f"{API_PREFIX}/health", {}, {}),
        (f"{API_PREFIX}/decisions", {}, SCENE_HEADERS),
        (f"{API_PREFIX}/decisions", {"page": "0"}, READ_HEADERS),
        (f"{API_PREFIX}/decisions", {"page_size": "101"}, READ_HEADERS),
        (f"{API_PREFIX}/decisions", {"status": "nope"}, READ_HEADERS),
        (
            f"{API_PREFIX}/decisions",
            {"created_from": "2026-10-01T00:00:00"},
            READ_HEADERS,
        ),
    ]
    for path, query, headers in requests:
        response = await env.client.get(path, headers=headers, params=query)
        observed.append(response.status_code)
        assert response.headers["cache-control"] == NO_STORE, (path, query)
        if response.status_code == 422:
            assert response.json() == {"detail": FIXED_QUERY_422_DETAIL}
        for token in ("synthetic", "Traceback", "psycopg", "asyncpg"):
            assert token not in response.text

    missing = await env.client.get(
        f"{API_PREFIX}/decisions/{uuid4()}", headers=READ_HEADERS
    )
    observed.append(missing.status_code)
    assert missing.headers["cache-control"] == NO_STORE
    duplicate = await env.client.get(
        f"{API_PREFIX}/decisions/{uuid4()}/body", headers=BODY_HEADERS
    )
    observed.append(duplicate.status_code)
    assert duplicate.headers["cache-control"] == NO_STORE

    env.probe.outage = True
    try:
        unavailable = await env.client.get(
            f"{API_PREFIX}/decisions", headers=READ_HEADERS
        )
    finally:
        env.probe.outage = False
    observed.append(unavailable.status_code)
    assert unavailable.json() == {"detail": FIXED_503_DETAIL}
    assert unavailable.headers["cache-control"] == NO_STORE

    malformed = await env.client.get(
        f"{API_PREFIX}/decisions/not-a-uuid-4f0c", headers=READ_HEADERS
    )
    observed.append(malformed.status_code)
    assert malformed.status_code == 422
    assert malformed.headers["cache-control"] == NO_STORE
    assert malformed.json() == {"detail": FIXED_IDENTIFIER_422_DETAIL}
    assert "not-a-uuid-4f0c" not in malformed.text

    assert sorted(set(observed)) == [200, 401, 403, 404, 422, 503]
    for response_payload in (unavailable.json(), malformed.json()):
        assert set(response_payload) == {"detail"}
