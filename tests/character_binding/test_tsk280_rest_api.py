"""TSK-280 真实 FastAPI 修复路由契约（无服务阶段为缺失业务模块 RED）。

生产 ``management_api`` 路由模块尚未实现：本文件顶层 import 失败即基线 RED
证据；实现落地后这些用例固定 REST 控制面（权限分权、理由/请求 ID、错误映射、
审计脱敏与路由闭集），与 ``TSK-280-contract.md`` 一致。路由经
``service_getter`` 注入桩服务，仅观察真实 HTTP seam。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi import FastAPI
from komari_bot.plugins.character_binding.repair import (
    RepairBlockedByGameError,
    RepairDependencyChangedError,
    RepairTargetNotFoundError,
    RepairTokenError,
)

from komari_bot.plugins.character_binding import management_api
from tests.character_binding.tsk280_support import (
    MANAGE_CREDENTIALS,
    READ_CREDENTIALS,
    REVOKED_MANAGE_CREDENTIALS,
    WILDCARD_CREDENTIALS,
    Scope,
    StubBindingRepairService,
    confirm_result_payload,
    diagnosis_payload,
    member_view,
    preview_payload,
    read_headers,
    safe_target_hash,
    write_headers,
)
from tests.character_binding.tsk280_support import (
    scope as make_scope,
)

if TYPE_CHECKING:
    from nonebug import App

    from komari_bot.management.management_audit import (
        ManagementAuditEvent,
        ManagementAuditRecorder,
    )


def _build_app(
    service: StubBindingRepairService,
    audit_events: list[ManagementAuditEvent] | None = None,
    *,
    api_token: list[dict[str, object]] = WILDCARD_CREDENTIALS,
) -> FastAPI:
    if audit_events is None:
        return _build_app_with_recorder(service, None, api_token=api_token)

    async def _record_audit(event: ManagementAuditEvent) -> None:
        audit_events.append(event)

    return _build_app_with_recorder(service, _record_audit, api_token=api_token)


def _build_app_with_recorder(
    service: StubBindingRepairService,
    recorder: ManagementAuditRecorder | None,
    *,
    api_token: list[dict[str, object]] = WILDCARD_CREDENTIALS,
) -> FastAPI:
    app = FastAPI()
    management_api.register_character_binding_repair_api(
        app,
        api_token=api_token,
        allowed_origins=[],
        service_getter=lambda: service,
        audit_recorder=recorder,
    )
    return app


def _diagnose_url(current: Scope) -> str:
    return (
        f"{management_api.API_PREFIX}/diagnose"
        f"?app_id={current.app_id}&group_openid={current.group_openid}"
    )


@pytest.mark.asyncio
async def test_diagnose_route_requires_read_permission_and_returns_diagnosis(
    app: App,
) -> None:
    """AC：未授权不可读身份；character_binding:read 只允许诊断。"""
    service = StubBindingRepairService()
    current = make_scope("rest-diagnose")
    service.diagnosis = diagnosis_payload(
        current,
        members=[member_view(current, 1), member_view(current, 2, name="星野")],
    )

    async with app.test_server(
        asgi=cast("Any", _build_app(service, api_token=READ_CREDENTIALS))
    ) as ctx:
        client = ctx.get_client()
        anonymous = await client.get(_diagnose_url(current))
        authorized = await client.get(_diagnose_url(current), headers=read_headers())

    assert anonymous.status_code == 401
    assert authorized.status_code == 200
    payload = authorized.json()
    assert payload["app_id"] == current.app_id
    assert payload["group_openid"] == current.group_openid
    assert payload["group_id"] == current.group_id
    assert payload["game_present"] is False
    assert payload["game_lifecycle"] is None
    assert len(payload["members"]) == 2
    assert payload["members"][0]["member_qq"] == current.with_member(1).member_qq
    assert service.diagnose_calls == [
        {"app_id": current.app_id, "group_openid": current.group_openid}
    ]


@pytest.mark.asyncio
async def test_read_only_credential_cannot_preview_or_confirm(app: App) -> None:
    """AC：read 不蕴含 manage，只读凭据禁止预览/确认清除。"""
    service = StubBindingRepairService()
    current = make_scope("rest-readonly")
    service.preview_result = preview_payload(current)
    service.confirm_result = confirm_result_payload(current)

    async with app.test_server(
        asgi=cast("Any", _build_app(service, api_token=READ_CREDENTIALS))
    ) as ctx:
        client = ctx.get_client()
        headers = write_headers(request_id="readonly-write", token="reader-token-00000000")
        preview = await client.post(
            f"{management_api.API_PREFIX}/preview",
            headers=headers,
            json={"app_id": current.app_id, "group_openid": current.group_openid},
        )
        confirm = await client.post(
            f"{management_api.API_PREFIX}/confirm",
            headers=headers,
            json={
                "app_id": current.app_id,
                "group_openid": current.group_openid,
                "token": "preview-token-1",
            },
        )

    assert preview.status_code == 403
    assert confirm.status_code == 403
    assert service.preview_calls == []
    assert service.confirm_calls == []


@pytest.mark.asyncio
async def test_manage_credential_can_preview_confirm_and_read(app: App) -> None:
    """AC：manage 才可预览/确认；按共享蕴含约定 manage 亦含 read。"""
    service = StubBindingRepairService()
    current = make_scope("rest-manage")
    service.diagnosis = diagnosis_payload(current, members=[member_view(current)])
    service.preview_result = preview_payload(current)
    service.confirm_result = confirm_result_payload(current)

    async with app.test_server(
        asgi=cast("Any", _build_app(service, api_token=MANAGE_CREDENTIALS))
    ) as ctx:
        client = ctx.get_client()
        diagnose = await client.get(
            _diagnose_url(current),
            headers=read_headers("operator-token-000000"),
        )
        preview = await client.post(
            f"{management_api.API_PREFIX}/preview",
            headers=write_headers(
                request_id="manage-preview", token="operator-token-000000"
            ),
            json={"app_id": current.app_id, "group_openid": current.group_openid},
        )
        confirm = await client.post(
            f"{management_api.API_PREFIX}/confirm",
            headers=write_headers(
                request_id="manage-confirm", token="operator-token-000000"
            ),
            json={
                "app_id": current.app_id,
                "group_openid": current.group_openid,
                "token": "preview-token-1",
            },
        )

    assert diagnose.status_code == 200
    assert preview.status_code == 200
    assert confirm.status_code == 200
    assert service.confirm_calls[0]["operator_id"] == "binding-operator"


@pytest.mark.asyncio
async def test_preview_and_confirm_require_reason_and_request_id(app: App) -> None:
    """AC：变更必须有理由；写操作必须携带审计请求 ID。"""
    service = StubBindingRepairService()
    current = make_scope("rest-reason")
    service.preview_result = preview_payload(current)
    service.confirm_result = confirm_result_payload(current)

    async with app.test_server(asgi=cast("Any", _build_app(service))) as ctx:
        client = ctx.get_client()
        base_headers = {"Authorization": "Bearer wildcard-token-00000"}
        body = {"app_id": current.app_id, "group_openid": current.group_openid}
        preview_no_reason = await client.post(
            f"{management_api.API_PREFIX}/preview",
            headers={**base_headers, "X-Request-ID": "r1"},
            json=body,
        )
        preview_no_request_id = await client.post(
            f"{management_api.API_PREFIX}/preview",
            headers={**base_headers, "X-Komari-Change-Reason": "运营核对"},
            json=body,
        )
        confirm_no_reason = await client.post(
            f"{management_api.API_PREFIX}/confirm",
            headers={**base_headers, "X-Request-ID": "r2"},
            json={**body, "token": "preview-token-1"},
        )

    assert preview_no_reason.status_code == 400
    assert preview_no_request_id.status_code == 400
    assert confirm_no_reason.status_code == 400
    assert service.preview_calls == []
    assert service.confirm_calls == []


@pytest.mark.asyncio
async def test_preview_reports_scope_count_names_and_token(app: App) -> None:
    """AC：预览明确范围/数量/将被清除的角色名，并签发令牌。"""
    service = StubBindingRepairService()
    current = make_scope("rest-preview")
    service.preview_result = preview_payload(
        current,
        scope="group",
        member_openid=None,
        affected_count=3,
        cleared_names=("甲", "乙", "丙"),
        version="fingerprint-abc",
    )

    async with app.test_server(asgi=cast("Any", _build_app(service))) as ctx:
        client = ctx.get_client()
        response = await client.post(
            f"{management_api.API_PREFIX}/preview",
            headers=write_headers(request_id="preview-group"),
            json={"app_id": current.app_id, "group_openid": current.group_openid},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["scope"] == "group"
    assert payload["member_openid"] is None
    assert payload["affected_count"] == 3
    assert payload["cleared_names"] == ["甲", "乙", "丙"]
    assert payload["token"]
    assert payload["version"] == "fingerprint-abc"
    assert payload["expires_at"]
    preview_call = service.preview_calls[0]
    assert preview_call["operator_id"] == "binding-wildcard"
    assert preview_call.get("member_openid") is None
    assert preview_call["reason"] == "运营核对错误关联"


@pytest.mark.asyncio
async def test_confirm_clears_and_returns_result(app: App) -> None:
    """确认清除返回实际清除数量/名字，并把操作者/请求 ID/理由/令牌传入服务。"""
    service = StubBindingRepairService()
    current = make_scope("rest-confirm")
    service.confirm_result = confirm_result_payload(
        current,
        cleared_count=2,
        cleared_names=("甲", "乙"),
    )

    async with app.test_server(asgi=cast("Any", _build_app(service))) as ctx:
        client = ctx.get_client()
        response = await client.post(
            f"{management_api.API_PREFIX}/confirm",
            headers=write_headers(request_id="confirm-ok"),
            json={
                "app_id": current.app_id,
                "group_openid": current.group_openid,
                "token": "preview-token-1",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["cleared_count"] == 2
    assert payload["cleared_names"] == ["甲", "乙"]
    assert payload["scope"] == "member"
    confirm_call = service.confirm_calls[0]
    assert confirm_call["operator_id"] == "binding-wildcard"
    assert confirm_call["request_id"] == "confirm-ok"
    assert confirm_call["reason"] == "运营核对错误关联"
    assert confirm_call["token"] == "preview-token-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (RepairTargetNotFoundError("未找到目标群映射"), 404),
        (RepairTokenError("令牌无效"), 422),
        (RepairDependencyChangedError("状态已变化"), 409),
        (RepairBlockedByGameError("对局存在"), 409),
    ],
)
async def test_repair_error_paths_map_to_fixed_status_codes(
    app: App,
    error: BaseException,
    status_code: int,
) -> None:
    """错误映射固定：未找到 404、令牌 422、依赖变化/对局阻断 409，均不写库。"""
    service = StubBindingRepairService()
    service.error = error
    current = make_scope("rest-error")
    service.preview_result = preview_payload(current)

    async with app.test_server(asgi=cast("Any", _build_app(service))) as ctx:
        client = ctx.get_client()
        preview = await client.post(
            f"{management_api.API_PREFIX}/preview",
            headers=write_headers(request_id="preview-error"),
            json={"app_id": current.app_id, "group_openid": current.group_openid},
        )
        confirm = await client.post(
            f"{management_api.API_PREFIX}/confirm",
            headers=write_headers(request_id="confirm-error"),
            json={
                "app_id": current.app_id,
                "group_openid": current.group_openid,
                "token": "preview-token-1",
            },
        )

    assert preview.status_code == status_code
    assert confirm.status_code == status_code


@pytest.mark.asyncio
async def test_audit_records_safe_fields_only(app: App) -> None:
    """审计记录操作者/理由/请求 ID/目标哈希/版本/数量/结果，绝不落原始身份。"""
    service = StubBindingRepairService()
    current = make_scope("rest-audit")
    service.preview_result = preview_payload(
        current,
        scope="member",
        version="fingerprint-abc",
    )
    service.confirm_result = confirm_result_payload(current)
    audit_events: list[ManagementAuditEvent] = []

    async with app.test_server(
        asgi=cast("Any", _build_app(service, audit_events))
    ) as ctx:
        client = ctx.get_client()
        await client.post(
            f"{management_api.API_PREFIX}/preview",
            headers=write_headers(request_id="audit-preview"),
            json={"app_id": current.app_id, "group_openid": current.group_openid},
        )
        await client.post(
            f"{management_api.API_PREFIX}/confirm",
            headers=write_headers(request_id="audit-confirm"),
            json={
                "app_id": current.app_id,
                "group_openid": current.group_openid,
                "token": "preview-token-1",
            },
        )

    assert [event.action for event in audit_events] == [
        "character_binding.repair.preview",
        "character_binding.repair.preview",
        "character_binding.repair.confirm",
        "character_binding.repair.confirm",
    ]
    preview_success = audit_events[1]
    confirm_success = audit_events[3]
    assert preview_success.operator_id == "binding-wildcard"
    assert preview_success.reason == "运营核对错误关联"
    assert preview_success.request_id == "audit-preview"
    assert preview_success.target_hash == safe_target_hash(current)
    assert preview_success.metadata["scope"] == "member"
    assert preview_success.metadata["version"] == "fingerprint-abc"
    assert preview_success.metadata["affected_count"] == 1
    assert preview_success.metadata["result_code"] == "preview_issued"
    assert confirm_success.operator_id == "binding-wildcard"
    assert confirm_success.request_id == "audit-confirm"
    assert confirm_success.target_hash == safe_target_hash(current)
    assert confirm_success.metadata["scope"] == "member"
    assert confirm_success.metadata["cleared_count"] == 1
    assert confirm_success.metadata["result_code"] == "cleared"

    rendered = json.dumps(
        [event.to_dict() for event in audit_events],
        ensure_ascii=False,
        sort_keys=True,
    )
    assert current.app_id not in rendered
    assert current.group_openid not in rendered
    assert current.member_openid not in rendered
    assert current.member_qq not in rendered
    assert "花火" not in rendered


@pytest.mark.asyncio
async def test_revoked_manage_credential_cannot_access_repair(app: App) -> None:
    """AC：已撤销凭据即使带 manage 权限也不能读写身份关系。"""
    service = StubBindingRepairService()
    current = make_scope("rest-revoked")
    service.diagnosis = diagnosis_payload(current, members=[member_view(current)])
    service.preview_result = preview_payload(current)
    service.confirm_result = confirm_result_payload(current)
    headers = write_headers(
        request_id="revoked-write", token="revoked-token-000000"
    )

    async with app.test_server(
        asgi=cast("Any", _build_app(service, api_token=REVOKED_MANAGE_CREDENTIALS))
    ) as ctx:
        client = ctx.get_client()
        diagnose = await client.get(
            _diagnose_url(current), headers=read_headers("revoked-token-000000")
        )
        preview = await client.post(
            f"{management_api.API_PREFIX}/preview",
            headers=headers,
            json={"app_id": current.app_id, "group_openid": current.group_openid},
        )
        confirm = await client.post(
            f"{management_api.API_PREFIX}/confirm",
            headers=headers,
            json={
                "app_id": current.app_id,
                "group_openid": current.group_openid,
                "token": "preview-token-1",
            },
        )

    assert diagnose.status_code == 401
    assert preview.status_code == 401
    assert confirm.status_code == 401
    assert service.diagnose_calls == []
    assert service.preview_calls == []
    assert service.confirm_calls == []


class _SelectiveRaisingAuditRecorder:
    """在指定调用序号抛出的审计 recorder（started=1，final=2）。"""

    def __init__(self, raise_on_calls: set[int]) -> None:
        self.raise_on_calls = raise_on_calls
        self.calls = 0
        self.events: list[ManagementAuditEvent] = []

    async def __call__(self, event: ManagementAuditEvent) -> None:
        self.calls += 1
        if self.calls in self.raise_on_calls:
            msg = "audit sink CANARY-unavailable"
            raise RuntimeError(msg)
        self.events.append(event)


@pytest.mark.asyncio
async def test_audit_start_failure_aborts_without_operation(app: App) -> None:
    """审计启动事件失败 → 请求失败且业务不执行（不签发令牌/不删除）。"""
    service = StubBindingRepairService()
    current = make_scope("rest-audit-start-fail")
    service.preview_result = preview_payload(current)
    service.confirm_result = confirm_result_payload(current)
    recorder = _SelectiveRaisingAuditRecorder({1})

    async with app.test_server(
        asgi=cast("Any", _build_app_with_recorder(service, recorder))
    ) as ctx:
        client = ctx.get_client()
        preview = await client.post(
            f"{management_api.API_PREFIX}/preview",
            headers=write_headers(request_id="audit-start-preview"),
            json={"app_id": current.app_id, "group_openid": current.group_openid},
        )
        confirm = await client.post(
            f"{management_api.API_PREFIX}/confirm",
            headers=write_headers(request_id="audit-start-confirm"),
            json={
                "app_id": current.app_id,
                "group_openid": current.group_openid,
                "token": "preview-token-1",
            },
        )

    assert preview.status_code == 500
    assert confirm.status_code == 500
    assert service.preview_calls == []
    assert service.confirm_calls == []


@pytest.mark.asyncio
async def test_audit_final_failure_does_not_break_operation(app: App) -> None:
    """审计结果事件失败被共享 span 吞掉：操作成功、结果正确、业务各执行一次。"""
    service = StubBindingRepairService()
    current = make_scope("rest-audit-final-fail")
    service.preview_result = preview_payload(current)
    service.confirm_result = confirm_result_payload(current)
    recorder = _SelectiveRaisingAuditRecorder({2, 4})

    async with app.test_server(
        asgi=cast("Any", _build_app_with_recorder(service, recorder))
    ) as ctx:
        client = ctx.get_client()
        preview = await client.post(
            f"{management_api.API_PREFIX}/preview",
            headers=write_headers(request_id="audit-final-preview"),
            json={"app_id": current.app_id, "group_openid": current.group_openid},
        )
        confirm = await client.post(
            f"{management_api.API_PREFIX}/confirm",
            headers=write_headers(request_id="audit-final-confirm"),
            json={
                "app_id": current.app_id,
                "group_openid": current.group_openid,
                "token": "preview-token-1",
            },
        )

    assert preview.status_code == 200
    assert confirm.status_code == 200
    assert preview.json()["token"] == "preview-token-1"
    assert confirm.json()["cleared_count"] == 1
    assert len(service.preview_calls) == 1
    assert len(service.confirm_calls) == 1
    assert [event.outcome for event in recorder.events] == ["started", "started"]


def test_route_set_is_fixed_without_identity_repoint() -> None:
    """路由闭集恰好三个；不存在改指/重定向/直接写身份的路径。"""
    app = _build_app(StubBindingRepairService())
    repair_routes = sorted(
        (
            ",".join(sorted(getattr(route, "methods", None) or [])),
            getattr(route, "path", ""),
        )
        for route in app.routes
        if str(getattr(route, "path", "")).startswith(management_api.API_PREFIX)
    )
    assert repair_routes == [
        ("POST", f"{management_api.API_PREFIX}/confirm"),
        ("GET", f"{management_api.API_PREFIX}/diagnose"),
        ("POST", f"{management_api.API_PREFIX}/preview"),
    ]


def test_registration_does_not_require_group_admission_or_roulette_runtime() -> None:
    """受限群/轮盘关闭下控制面仍可达：注册不依赖准入或轮盘运行时。"""
    module_path = Path(management_api.__file__)
    source = module_path.read_text(encoding="utf-8")
    assert "group_admission" not in source
    assert "komari_roulette" not in source
    app = _build_app(StubBindingRepairService())
    assert app is not None
