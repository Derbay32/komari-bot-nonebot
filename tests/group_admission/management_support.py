"""TSK-223 阶段 A 管理控制面测试共享基础设施（测试专用，不承载生产语义）。

承载 ``register_group_admission_api`` 的 API 契约验收所需的确定性装配：

- 真实 ``ConfigManager`` + 无服务可控 ``AdmissionStorageFake``；
- 管理凭据矩阵（``config:read`` / ``config:write`` / ``*``）；
- 注入式审计 recorder（逐事件快照 module singleton 运行时状态，用于证明
  「本地发布先于审计 final/响应」的顺序契约）；
- 规范化策略 SHA-256 指纹的独立计算（审计安全字段的参照真源）。

冻结的测试接缝（生产实现必须满足，否则用例红）：

1. 控制面在 **请求时** 经 ``runtime._runtime`` module 属性惰性解析准入运
   行时（与顶层 ``adjudicate`` / ``get_runtime_state`` 同一接缝）；真实
   ``ConfigManager`` 由 ``_AdmissionRuntime.start(manager)`` 注入并归运行
   时内部拥有：控制面不在请求时查找 config_manager 全局注册表，不自建管
   理器实例，也不在注册期缓存管理器；
2. GET 强制持久刷新与 PUT 严格 CAS 经运行时内部异步控制面完成，其方法名
   不受冻结：测试只观察 HTTP 响应、存储 fake 计数与 ``get_runtime_state()``
   状态投影等外部效果，不引用也不约定任何私有控制面方法名；
3. ``register_group_admission_api`` 是唯一装配入口，签名冻结为
   ``(app, *, api_token, allowed_origins, audit_recorder=None)``。

生产符号缺失时，``prepare_control_plane`` 在 ``import_admission_package``
处抛 ``AttributeError``/``ModuleNotFoundError``，用例以缺失生产能力红。
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from komari_bot.plugins.config_manager import manager as manager_module
from komari_bot.plugins.config_manager.manager import ConfigManager
from tests.group_admission.runtime_support import (
    PLUGIN_NAME,
    AdmissionStorageFake,
    import_admission_package,
    import_runtime_module,
    install_singleton,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    import httpx
    import pytest
    from fastapi import FastAPI

    from komari_bot.management.management_audit import ManagementAuditEvent

POLICY_PATH = "/api/v2/group-admission/policy"
STATUS_PATH = "/api/v2/group-admission/status"

READER_TOKEN = "reader-token-00000000"
WRITER_TOKEN = "writer-token-00000000"
ADMIN_TOKEN = "admin-token-000000000"

#: PUT 审计 final 事件 metadata 的精确键集（冻结契约）。
#: 值域：old/new revision（冲突等无新修订场景 new_revision 为 None）、
#: old/new 规范化策略 SHA-256 指纹、persisted/published 布尔、
#: result_code（成功为 "succeeded"，失败为 7 个封闭错误码之一）。
EXPECTED_AUDIT_METADATA_KEYS = frozenset(
    {
        "old_revision",
        "new_revision",
        "old_policy_fingerprint",
        "new_policy_fingerprint",
        "persisted",
        "published",
        "result_code",
    }
)

RESULT_CODE_SUCCESS = "succeeded"

#: 本 Module 409/422/503/500 错误体 code 封闭白名单（冻结）。
CLOSED_ERROR_CODES = frozenset(
    {
        "invalid_if_match",
        "invalid_policy",
        "revision_conflict",
        "storage_unavailable",
        "stored_policy_invalid",
        "snapshot_publish_failed",
        "internal_error",
    }
)

#: 审计动作/资源标识（冻结，参照 user_ban 的 "<plugin>.<action>" 惯例）。
FROZEN_AUDIT_ACTION = "group_admission.update_policy"
FROZEN_AUDIT_RESOURCE = "group_admission"


def assert_whitelist_detail(
    response: httpx.Response,
    status_code: int,
    code: str,
    *,
    configured_revision: int | None = None,
    effective_revision: int | None = None,
    persisted: bool = False,
) -> dict[str, Any]:
    """断言本 Module 错误体使用精确白名单外壳并返回 detail。

    ``configured_revision`` / ``effective_revision`` 投影运行时 singleton 在
    响应构造时点的当前值（本请求触发的状态转换已应用）：ready@N 下的参数/
    策略/冲突/存储写失败/审计启动内部错误/GET 存储失败均为 N/N；冷启动非
    法持久策略为 configured=N/effective=None；从未启动/无管理器为 None/None。
    字段集固定白名单，错误体绝不返回 policy。
    """
    assert code in CLOSED_ERROR_CODES, f"测试自身使用了非封闭 code: {code}"
    assert response.status_code == status_code, response.text
    body = response.json()
    assert set(body) == {"detail"}, f"错误响应外壳不精确: {body}"
    detail = body["detail"]
    assert set(detail) == {
        "code",
        "message",
        "configured_revision",
        "effective_revision",
        "persisted",
    }, f"错误 detail 键集不精确: {detail}"
    assert detail["code"] == code
    assert isinstance(detail["message"], str) and detail["message"].strip()
    assert detail["configured_revision"] == configured_revision, detail
    assert detail["effective_revision"] == effective_revision, detail
    assert detail["persisted"] is persisted, detail
    return detail


def atomic_policy(mode: str, group_ids: Sequence[int]) -> dict[str, object]:
    """构造一个完整原子策略对象。"""
    return {"mode": mode, "group_ids": list(group_ids)}


def normalized_policy(mode: str, group_ids: Sequence[int]) -> dict[str, object]:
    """构造规范化策略（确定性去重排序），即 GET/PUT 成功响应的期望值。"""
    return {"mode": mode, "group_ids": sorted(set(group_ids))}


def canonical_policy_fingerprint(policy: Mapping[str, object]) -> str:
    """规范化策略的 SHA-256 指纹（审计安全字段的独立参照真源）。

    规范化序列化：键排序、紧凑分隔符、保留非 ASCII；只含规范化后的策略
    对象（``group_ids`` 已去重排序），绝不包含群号明文。
    """
    canonical = json.dumps(
        dict(policy),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def management_credentials() -> list[dict[str, object]]:
    """三凭据矩阵：只读 / 只写 / 通配（权限名遵循共享鉴权依赖）。"""
    return [
        {
            "credential_id": "reader",
            "token": READER_TOKEN,
            "permissions": ["config:read"],
        },
        {
            "credential_id": "writer",
            "token": WRITER_TOKEN,
            "permissions": ["config:write"],
        },
        {
            "credential_id": "admin",
            "token": ADMIN_TOKEN,
            "permissions": ["*"],
        },
    ]


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def put_headers(
    token: str,
    *,
    if_match: str | None,
    change_reason: str | None = "acceptance-test-update-policy",
    request_id: str | None = "ga-put-0001",
) -> dict[str, str]:
    headers = auth_headers(token)
    if if_match is not None:
        headers["If-Match"] = if_match
    if change_reason is not None:
        headers["X-Komari-Change-Reason"] = change_reason
    if request_id is not None:
        headers["X-Request-ID"] = request_id
    return headers


class RecordingAuditRecorder:
    """注入式审计 recorder：收集事件并在事件时点快照运行时状态。

    ``runtime_state_observations`` 与 ``events`` 逐事件对齐，用于证明
    「publication success 必须在 audit final/success 观察时 runtime
    effective 已是 new revision」（本地发布先于响应/审计完成）。
    """

    def __init__(self) -> None:
        self.events: list[ManagementAuditEvent] = []
        self.runtime_state_observations: list[Any] = []

    async def __call__(self, event: ManagementAuditEvent) -> None:
        self.events.append(event)
        admission = import_admission_package()
        self.runtime_state_observations.append(admission.get_runtime_state())

    def final_events(self) -> list[ManagementAuditEvent]:
        return [event for event in self.events if event.outcome != "started"]


def asgi_client(app: FastAPI) -> httpx.AsyncClient:
    """ASGI 内存客户端（不启动真实服务、不触发 lifespan）。"""
    import httpx

    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport, base_url="http://group-admission.test"
    )


async def prepare_control_plane(
    monkeypatch: pytest.MonkeyPatch,
    storage: AdmissionStorageFake,
    *,
    audit_recorder: Any | None = None,
    allowed_origins: Sequence[str] = (),
    credentials: Sequence[Mapping[str, object]] | None = None,
    runtime_kwargs: Mapping[str, object] | None = None,
) -> tuple[FastAPI, Any, ConfigManager]:
    """装配控制面验收环境并返回 ``(app, runtime, manager)``。

    步骤（全部经既有冻结接缝，不新增生产测试钩子）：

    1. monkeypatch 存储工厂为可控 fake；
    2. 构造真实 ``ConfigManager``，经 ``runtime.start(manager)`` 注入，归运
       行时内部拥有（不写入 config_manager 全局注册表）；
    3. 安装真实 ``_AdmissionRuntime`` 为 module singleton；
    4. 经公开 ``register_group_admission_api`` 装配 FastAPI 应用。

    TSK-223 阶段 B：``runtime_kwargs`` 原样透传给 ``_AdmissionRuntime`` 构
    造器（可控 UTC 时钟 / 在线 Bot 提供者 / SUPERUSERS 提供者），观测类用例
    经此注入确定性时钟与通知投递环境。

    返回的 ``manager`` 仅供测试侧既有接缝使用（如摘除快照 listener 模拟
    持久化未发布），不暗示控制面从测试取得管理器。
    """
    monkeypatch.setattr(manager_module, "get_config_storage", lambda: storage)

    from tests.group_admission.runtime_support import AdmissionValueSchema

    manager = ConfigManager(PLUGIN_NAME, AdmissionValueSchema)

    runtime_module = import_runtime_module()
    runtime = runtime_module._AdmissionRuntime(**(runtime_kwargs or {}))
    await runtime.start(manager)
    install_singleton(monkeypatch, runtime)

    admission = import_admission_package()
    from fastapi import FastAPI

    app = FastAPI()
    admission.register_group_admission_api(
        app,
        api_token=list(
            credentials if credentials is not None else management_credentials()
        ),
        allowed_origins=list(allowed_origins),
        audit_recorder=audit_recorder,
    )
    return app, runtime, manager
