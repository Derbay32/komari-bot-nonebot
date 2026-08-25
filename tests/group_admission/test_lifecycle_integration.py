"""TSK-248 生产装配后的入口门禁 / 管理控制面 / Router 单挂载验收（AC3/AC4/AC9）。

生产 seam：

- group_admission 顶层公共接口与 event-preprocessor registry（AC3）；
- 管理 ASGI API 公开 policy/status 路由及路由表（AC4/AC9）；
- 生产装配架构边界 AST 约束（AC9：生命周期不得重复挂载 Router，不能只做
  字符串存在性断言——本文件同时以行为测试证明路由只挂载一次）。

- AC3 ready 后获准群业务通过 event gate，受限群静默拒绝；
- AC4 policy GET/PUT 访问真实持久配置，status 对三态返回安全投影；
- AC9 Router 仍只装配一次。

修复前红基线：旧实现生产代码未装配 driver startup hook，
``lifecycle_context`` 捕获不到钩子，本文件用例在调用 startup 钩子处失败
（red）；当前测试用于防止该缺陷回归。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from nonebot.adapters.onebot.v11.event import GroupMessageEvent

from tests.group_admission.entry_gate_support import (
    ProbeBot,
    dispatch,
    make_v11_event,
    register_phase_probe,
)
from tests.group_admission.lifecycle_support import (
    invoke_hook,
    lifecycle_context,
    require_single_startup_hook,
)
from tests.group_admission.management_support import (
    ADMIN_TOKEN,
    POLICY_PATH,
    READER_TOKEN,
    STATUS_PATH,
    asgi_client,
    assert_whitelist_detail,
    auth_headers,
    management_credentials,
)
from tests.group_admission.observability_support import (
    assert_status_exact_shape,
)
from tests.group_admission.runtime_support import (
    AdmissionStorageFake,
    import_admission_package,
    stored_policy,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.group_admission_acceptance

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = PROJECT_ROOT / "komari_bot" / "plugins" / "group_admission"

BLACKLIST_EMPTY: dict[str, object] = {"mode": "blacklist", "group_ids": []}
BLACKLIST_100: dict[str, object] = {"mode": "blacklist", "group_ids": [100]}
INVALID_POLICY: dict[str, object] = {"mode": "graylist", "group_ids": []}


def _mount_api(app: FastAPI) -> None:
    """经公开装配入口挂载管理 Router（komari_management 的传统 mount 收尾）。"""
    admission = import_admission_package()
    admission.register_group_admission_api(
        app,
        api_token=management_credentials(),
        allowed_origins=[],
        audit_recorder=None,
    )


def _iter_api_routes(routes: object) -> Iterator[APIRoute]:
    """深度遍历路由树（与 test_management_registration 同构）。"""
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


def _group_admission_route_multiset(app: FastAPI) -> list[tuple[str, str]]:
    """group-admission 专属 APIRoute 的 ``(path, method)`` multiset。

    使用 list 保存重复条目：重复挂载会真实翻倍呈现，不被 set 去重掩盖。
    只统计 ``/api/v2/group-admission/`` 前缀下的路由。
    """
    return [
        (route.path, method)
        for route in _iter_api_routes(app.routes)
        if isinstance(route, APIRoute)
        and route.path.startswith("/api/v2/group-admission/")
        for method in sorted(route.methods or set())
    ]


# ---------------------------------------------------------------------------
# AC3：ready 后获准群业务通过 event gate，受限群静默拒绝
# ---------------------------------------------------------------------------


async def test_event_gate_after_driver_startup_admits_and_restricts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3：driver startup 后运行时 ready，事件门禁按策略裁决。

    获准群（200，不在黑名单）流经完整五阶段；受限群（100）静默拒绝、零阶
    段、零 Bot 调用，telemetry ``policy_restricted`` 恰 +1。
    """
    storage = AdmissionStorageFake(stored_policy(1, BLACKLIST_100))
    async with lifecycle_context(monkeypatch, storage) as ctx:
        await invoke_hook(require_single_startup_hook(ctx))
        assert ctx.runtime.get_state().is_ready is True

        bot = ProbeBot()
        trace: list[str] = []

        # 获准群：完整五阶段。
        register_phase_probe(trace, "message")
        await dispatch(bot, make_v11_event(GroupMessageEvent, group_id=200))
        assert trace == [
            "rule",
            "run_pre",
            "handler",
            "run_post",
            "event_post",
        ], f"获准群必须完整五阶段: trace={trace}"

        # 受限群：静默拒绝，零阶段、零 Bot 调用。
        trace.clear()
        await dispatch(bot, make_v11_event(GroupMessageEvent, group_id=100))
        assert trace == [], f"受限群必须被门禁拦截: trace={trace}"
        assert bot.calls == [], f"受限群不得触发任何 Bot 调用: {bot.calls}"

        # telemetry：两次裁决（获准 + 受限），policy_restricted 恰 +1
        # （经 status 公开投影读取）。
        app = FastAPI()
        _mount_api(app)
        async with asgi_client(app) as client:
            response = await client.get(
                STATUS_PATH, headers=auth_headers(READER_TOKEN)
            )
            assert response.status_code == 200, response.text
            telemetry = response.json()["telemetry"]
            assert telemetry["adjudications_total"] == 2, telemetry
            assert telemetry["by_reason_code"]["policy_restricted"] == 1, telemetry


# ---------------------------------------------------------------------------
# AC4：policy GET/PUT 访问真实持久配置，status 对三态返回安全投影
# ---------------------------------------------------------------------------


async def test_management_api_reads_and_writes_persistent_config_after_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC4：driver startup 后 GET/PUT 访问真实持久配置（经 runtime-owned
    manager，非 LKG / 非缓存）。

    GET 返回持久策略 + 强 ETag；PUT 严格 CAS 写回持久存储并发布本地快照，
    返回新 ETag；status 投影 ready 安全字段。
    """
    storage = AdmissionStorageFake(stored_policy(1, BLACKLIST_EMPTY))
    async with lifecycle_context(monkeypatch, storage) as ctx:
        await invoke_hook(require_single_startup_hook(ctx))

        app = FastAPI()
        _mount_api(app)

        async with asgi_client(app) as client:
            # GET：真实持久策略 + 强 ETag。
            get_response = await client.get(
                POLICY_PATH, headers=auth_headers(READER_TOKEN)
            )
            assert get_response.status_code == 200, get_response.text
            assert get_response.headers.get("etag") == '"1"'
            body = get_response.json()
            assert body["policy"] == {"mode": "blacklist", "group_ids": []}
            assert body["revision"] == 1
            assert "updated_at" in body

            # PUT：严格 CAS 写回持久存储，本地发布后返回新 ETag。
            put_response = await client.put(
                POLICY_PATH,
                headers={
                    **auth_headers(ADMIN_TOKEN),
                    "If-Match": '"1"',
                    "X-Komari-Change-Reason": "tsk-248 acceptance update",
                },
                json={"mode": "whitelist", "group_ids": [200]},
            )
            assert put_response.status_code == 200, put_response.text
            assert put_response.headers.get("etag") == '"2"'
            put_body = put_response.json()
            assert put_body["policy"] == {"mode": "whitelist", "group_ids": [200]}
            assert put_body["revision"] == 2

            # 持久存储已更新（真实持久配置，非内存）。
            assert storage.current_revision == 2
            assert storage.cas_calls, "PUT 必须经严格 CAS 写持久存储"

            # GET 再次读取到新持久策略。
            get_after = await client.get(
                POLICY_PATH, headers=auth_headers(READER_TOKEN)
            )
            assert get_after.status_code == 200, get_after.text
            assert get_after.json()["revision"] == 2

            # status：ready 安全投影。
            status_response = await client.get(
                STATUS_PATH, headers=auth_headers(READER_TOKEN)
            )
            assert status_response.status_code == 200, status_response.text
            status_body = status_response.json()
            assert_status_exact_shape(status_body)
            assert status_body["status"] == "ready"
            assert status_body["configured_revision"] == 2
            assert status_body["effective_revision"] == 2
            assert status_body["using_last_known_good"] is False


async def test_management_api_status_projects_three_states_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC4：status 对 ready / degraded / failed 三态返回 200 安全投影。

    投影只含冻结键集（无策略 / 群号 / 异常正文），三个状态各验一次。
    """
    # --- ready：合法策略经 driver startup 启动。 ---
    storage = AdmissionStorageFake(stored_policy(1, BLACKLIST_EMPTY))
    async with lifecycle_context(monkeypatch, storage) as ctx:
        await invoke_hook(require_single_startup_hook(ctx))
        app = FastAPI()
        _mount_api(app)
        async with asgi_client(app) as client:
            response = await client.get(
                STATUS_PATH, headers=auth_headers(READER_TOKEN)
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert_status_exact_shape(body)
            assert body["status"] == "ready"
            assert body["problem_code"] is None

    # --- degraded：更高非法持久修订经 watcher 投递 → LKG 降级。 ---
    storage = AdmissionStorageFake(stored_policy(1, BLACKLIST_100))
    async with lifecycle_context(monkeypatch, storage) as ctx:
        await invoke_hook(require_single_startup_hook(ctx))
        storage.deliver(stored_policy(2, INVALID_POLICY))
        assert ctx.runtime.get_state().is_ready is False

        app = FastAPI()
        _mount_api(app)
        async with asgi_client(app) as client:
            response = await client.get(
                STATUS_PATH, headers=auth_headers(READER_TOKEN)
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert_status_exact_shape(body)
            assert body["status"] == "degraded"
            assert body["problem_code"] == "stored_policy_invalid"
            assert body["configured_revision"] == 2
            assert body["effective_revision"] == 1
            assert body["using_last_known_good"] is True

    # --- failed：冷启动存储失败经 driver startup 收敛 failed。 ---
    storage = AdmissionStorageFake(
        fetch_error=RuntimeError("pg cold start failure")
    )
    async with lifecycle_context(monkeypatch, storage) as ctx:
        await invoke_hook(require_single_startup_hook(ctx))
        assert ctx.runtime.get_state().is_ready is False

        app = FastAPI()
        _mount_api(app)
        async with asgi_client(app) as client:
            response = await client.get(
                STATUS_PATH, headers=auth_headers(READER_TOKEN)
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert_status_exact_shape(body)
            assert body["status"] == "failed"
            assert body["problem_code"] == "storage_unavailable"
            assert body["configured_revision"] is None
            assert body["effective_revision"] is None

            # GET policy 故障关闭（不冒充持久配置）。
            get_response = await client.get(
                POLICY_PATH, headers=auth_headers(READER_TOKEN)
            )
            assert_whitelist_detail(get_response, 503, "storage_unavailable")


# ---------------------------------------------------------------------------
# AC9：Router 仍只装配一次
# ---------------------------------------------------------------------------


async def test_router_is_mounted_only_once_after_driver_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC9：lifecycle startup 前后同一管理 app 的专属路由 multiset 不变。

    生产生命周期不得新增第二挂载路径：startup 前经公开装配入口挂载一次
    （komari_management 的传统 mount 收尾），记录 group-admission 专属路由
    的 ``(path, method)`` multiset；driver startup 后重新统计同一 app 的该
    multiset，数量与方法集合必须与 startup 前完全一致。

    本断言不要求 ``register_group_admission_api`` 自身幂等（那是注册函数自身
    的契约，见 test_management_registration），也不经 set 去重判断数量——重复
    挂载会真实翻倍，multiset 相等即证明 startup 没有新增任何路由。
    """
    storage = AdmissionStorageFake(stored_policy(1, BLACKLIST_EMPTY))
    async with lifecycle_context(monkeypatch, storage) as ctx:
        app = FastAPI()
        _mount_api(app)
        before = _group_admission_route_multiset(app)
        assert sorted(before) == sorted(
            [
                (POLICY_PATH, "GET"),
                (POLICY_PATH, "PUT"),
                (STATUS_PATH, "GET"),
            ]
        ), f"startup 前公开装配入口必须恰有三个专属路由: {before}"

        await invoke_hook(require_single_startup_hook(ctx))

        after = _group_admission_route_multiset(app)
        assert after == before, (
            f"lifecycle startup 新增了 group-admission 路由: {after}"
        )


def test_lifecycle_assembly_does_not_mount_management_router() -> None:
    """AC9（AST）：生命周期装配不得重复挂载管理 Router。

    只扫描**调用**（``register_group_admission_api`` / ``include_router`` /
    ``add_api_route``），不扫描 import/def 文本；``management_api.py`` 是注
    册函数定义处，豁免。行为证据由
    ``test_router_is_mounted_only_once_after_driver_startup`` 提供——不是只
    做字符串存在性断言。
    """
    forbidden_calls = {"register_group_admission_api", "include_router", "add_api_route"}
    offenders: list[str] = []
    for module_file in sorted(PACKAGE_DIR.rglob("*.py")):
        if module_file.name == "management_api.py":
            continue  # 注册函数定义所在模块豁免
        tree = ast.parse(
            module_file.read_text(encoding="utf-8"), filename=str(module_file)
        )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else None
            if name is None and isinstance(func, ast.Attribute):
                name = func.attr
            if name in forbidden_calls:
                offenders.append(
                    f"{module_file.name}:{node.lineno}:{name}"
                )
    assert offenders == [], (
        f"生命周期装配出现管理 Router 挂载调用（Router 只能由 komari_management 挂载一次）: {offenders}"
    )
