"""TSK-223 阶段 A：注册幂等、CORS 复用与专属路由面验收。

验收目标（冻结）：

- ``register_group_admission_api`` 重复注册幂等：同一应用只出现三个专属端
  点（``GET/PUT /api/v2/group-admission/policy``、``GET
  /api/v2/group-admission/status``），不重复挂载；
- 复用 ``ensure_management_cors``：同源白名单重复注册不抛错、不重复挂载中
  间件；允许 Origin 的预检请求取得 CORS 头；
- 路由 / OpenAPI 面只含三个专属端点，无 reload / force / bypass / 通用字段
  写入口；
- module singleton 为从未启动的真实运行时时，控制面必须故障关闭（503）且零存
  储 I/O，不得崩溃；不向任何私有注册表写入。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from komari_bot.plugins.config_manager import manager as manager_module
from tests.group_admission.management_support import (
    ADMIN_TOKEN,
    POLICY_PATH,
    READER_TOKEN,
    STATUS_PATH,
    asgi_client,
    assert_whitelist_detail,
    atomic_policy,
    auth_headers,
    management_credentials,
    prepare_control_plane,
)
from tests.group_admission.observability_support import (
    assert_status_exact_shape,
    assert_telemetry_closed_maps,
)
from tests.group_admission.runtime_support import (
    AdmissionStorageFake,
    import_admission_package,
    import_runtime_module,
    install_singleton,
    stored_policy,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    import pytest as pytest_types

pytestmark = pytest.mark.group_admission_acceptance

EXPECTED_ROUTES = {
    (POLICY_PATH, "GET"),
    (POLICY_PATH, "PUT"),
    (STATUS_PATH, "GET"),
}


def _iter_api_routes(
    routes: object,
) -> Iterator[APIRoute]:
    """深度遍历路由树：FastAPI 0.139 起 include_router 将子路由包进
    ``_IncludedRouter``（``original_router.routes``）而非平铺到父应用。"""
    for route in routes:  # type: ignore[union-attr]
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


def _registered_api_routes(app: FastAPI) -> set[tuple[str, str]]:
    return {
        (route.path, method)
        for route in _iter_api_routes(app.routes)
        for method in (route.methods or set())
    }


async def test_registration_is_idempotent_with_exactly_three_routes(
    monkeypatch: pytest_types.MonkeyPatch,
) -> None:
    """重复注册不得重复挂载；路由面精确等于三个专属端点。"""
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [])))
    app, _runtime, _manager = await prepare_control_plane(monkeypatch, storage)

    admission = import_admission_package()

    # 第二次、第三次注册必须幂等
    for _index in range(2):
        admission.register_group_admission_api(
            app,
            api_token=management_credentials(),
            allowed_origins=[],
            audit_recorder=None,
        )

    assert _registered_api_routes(app) == EXPECTED_ROUTES

    openapi = app.openapi()
    assert set(openapi["paths"]) == {POLICY_PATH, STATUS_PATH}
    assert set(openapi["paths"][POLICY_PATH]) == {"get", "put"}
    assert set(openapi["paths"][STATUS_PATH]) == {"get"}
    for path in openapi["paths"]:
        lowered = path.lower()
        for forbidden in ("reload", "force", "bypass", "field"):
            assert forbidden not in lowered, f"路由面出现禁止入口: {path}"


async def test_registration_reuses_shared_management_cors(
    monkeypatch: pytest_types.MonkeyPatch,
) -> None:
    """CORS 经共享 ensure_management_cors 挂载一次；重复注册一致不报错。"""
    origins = ["http://admin.example.com"]
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [])))
    app, _runtime, _manager = await prepare_control_plane(
        monkeypatch, storage, allowed_origins=origins
    )

    admission = import_admission_package()

    admission.register_group_admission_api(
        app,
        api_token=management_credentials(),
        allowed_origins=origins,
        audit_recorder=None,
    )

    assert app.state.komari_management_cors_origins == tuple(origins)
    cors_middlewares = [
        middleware
        for middleware in app.user_middleware
        if "CORSMiddleware" in str(middleware.cls)
    ]
    assert len(cors_middlewares) == 1, "CORS 中间件必须只挂载一次"

    async with asgi_client(app) as client:
        preflight = await client.options(
            POLICY_PATH,
            headers={
                "Origin": "http://admin.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        disallowed = await client.options(
            POLICY_PATH,
            headers={
                "Origin": "http://evil.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert preflight.status_code == 200
    assert (
        preflight.headers.get("access-control-allow-origin")
        == "http://admin.example.com"
    )
    assert "access-control-allow-origin" not in disallowed.headers


async def test_endpoints_fail_closed_when_runtime_not_started(
    monkeypatch: pytest_types.MonkeyPatch,
) -> None:
    """module singleton 是未启动的真实运行时：GET/PUT 故障关闭，status 仍 200。

    构造并安装一个 **未 start** 的真实 ``_AdmissionRuntime``（没有注入管理
    器，不写任何私有注册表）：控制面在请求时经 singleton 解析后发现运行时不具
    备控制面能力，GET/PUT 故障关闭 503（错误体投影运行时当前值 None/None，
    不触存储），status 仍 200 投影真实状态。
    """
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [])))
    monkeypatch.setattr(manager_module, "get_config_storage", lambda: storage)

    runtime_module = import_runtime_module()
    runtime = runtime_module._AdmissionRuntime()
    install_singleton(monkeypatch, runtime)

    admission = import_admission_package()

    app = FastAPI()
    admission.register_group_admission_api(
        app,
        api_token=management_credentials(),
        allowed_origins=[],
        audit_recorder=None,
    )

    async with asgi_client(app) as client:
        get_response = await client.get(
            POLICY_PATH, headers=auth_headers(READER_TOKEN)
        )
        put_response = await client.put(
            POLICY_PATH,
            headers={
                **auth_headers(ADMIN_TOKEN),
                "If-Match": '"1"',
                "X-Komari-Change-Reason": "fail-closed probe",
            },
            json={"mode": "blacklist", "group_ids": []},
        )
        status_response = await client.get(
            STATUS_PATH, headers=auth_headers(READER_TOKEN)
        )

    assert_whitelist_detail(get_response, 503, "storage_unavailable")
    assert_whitelist_detail(put_response, 503, "storage_unavailable")
    assert status_response.status_code == 200
    status_body = status_response.json()
    assert_status_exact_shape(status_body)
    assert status_body["status"] == "failed"
    # 未启动：快照时间与问题时间全部为 null，遥测预初始化全零
    for field_name in (
        "configured_updated_at",
        "effective_loaded_at",
        "last_refresh_attempt_at",
        "last_storage_success_at",
        "problem_since",
    ):
        assert status_body[field_name] is None, field_name
    assert_telemetry_closed_maps(status_body["telemetry"], total=0)
    assert storage.fetch_calls == 0, "故障关闭路径不得读取存储"
    assert storage.cas_calls == [], "故障关闭路径不得写入存储"
