"""TSK-231 管理面 reply fulfillment 目标准入红基线验收。

驱动真实 ``create_reply_fulfillment_router`` + 注入式 ``FakeOpsService``，安装脚本化
``adjudicate``，逐 AC 断言：

- confirm-delivered / not-delivered / resume 在受限态下整条效果不得执行（生产当前
  提交前不调用 adjudicate，故红态失败）；
- 对账/续跑 intent 为 ``fact_finalization``；未送达的资源释放走 ``technical_cleanup``，
  且从不重发回复正文；
- 列表在正文读取前按最小归属投影过滤，不泄漏受限群存在性。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI

from komari_bot.management.management_audit import ManagementAuditEvent
from komari_bot.plugins.komari_management.reply_fulfillment_api import (
    API_PREFIX,
    create_reply_fulfillment_router,
)

from tests.group_admission.chat_admission_support import ScriptedAdjudicate
from tests.group_admission.management_admission_support import (
    ALLOWED_GROUP_ID,
    MANAGEMENT_CREDENTIALS,
    RESTRICTED_GROUP_ID,
    FakeOpsService,
    asgi_client,
    auth_headers,
    install_scripted,
)

pytestmark = pytest.mark.group_admission_acceptance

FULFILLMENT_MANAGE_TOKEN = "fulfillment-manage-token-00"
HDR_MANAGE = auth_headers(FULFILLMENT_MANAGE_TOKEN, request_id="ff-req-tsk231")
HDR_READ = auth_headers(FULFILLMENT_MANAGE_TOKEN)


def _admitted_intents(scripted):
    return [intent for (_groups, intent) in scripted.calls]


def _build_app(service: FakeOpsService, audit: list[Any]) -> "FastAPI":
    async def _record(event: ManagementAuditEvent) -> None:
        audit.append(event)

    app = FastAPI()
    app.include_router(
        create_reply_fulfillment_router(
            api_token=MANAGEMENT_CREDENTIALS,
            service_getter=lambda: service,
            audit_recorder=_record,
        )
    )
    return app


async def test_confirm_delivered_restricted_does_not_execute(monkeypatch) -> None:
    """AC1 restricted: confirm-delivered 整条拒绝，下游不执行、不被授权。"""
    scripted = ScriptedAdjudicate("restricted")
    install_scripted(monkeypatch, scripted)
    service = FakeOpsService(group_id=RESTRICTED_GROUP_ID)
    audit: list[Any] = []
    async with asgi_client(_build_app(service, audit)) as client:
        resp = await client.post(
            f"{API_PREFIX}/fulfillments/reply-target-0001/confirm-delivered",
            headers=HDR_MANAGE,
            json={"platform_message_id": None},
        )
    assert resp.status_code == 403, "restricted 群应拒绝送达确认"
    assert service.delivered_calls == [], "restricted 下仍调用了 confirm_delivered"
    assert scripted.calls, "confirm-delivered 前未裁决"
    assert _admitted_intents(scripted) == [], "受限态不应被授权 fact_finalization"


async def test_confirm_delivered_admitted_consults_fact_finalization(monkeypatch) -> None:
    """AC2: confirm-delivered 以 FACT_FINALIZATION 对账，交付后终止不重发。"""
    scripted = ScriptedAdjudicate("admitted")
    install_scripted(monkeypatch, scripted)
    service = FakeOpsService(group_id=ALLOWED_GROUP_ID)
    async with asgi_client(_build_app(service, [])) as client:
        resp = await client.post(
            f"{API_PREFIX}/fulfillments/reply-target-0001/confirm-delivered",
            headers=HDR_MANAGE,
            json={"platform_message_id": "platform-1"},
        )
    assert resp.status_code in (200, 201)
    assert scripted.calls, "confirm-delivered 前未裁决"
    assert "fact_finalization" in _admitted_intents(scripted), "未按 FACT_FINALIZATION 裁决"
    assert service.delivered_calls == [("reply-target-0001", "platform-1")]


async def test_confirm_not_delivered_resource_release_is_technical_cleanup(monkeypatch) -> None:
    """AC2: 未送达/资源释放只做 TECHNICAL_CLEANUP，从不重发回复正文。"""
    scripted = ScriptedAdjudicate("admitted")
    install_scripted(monkeypatch, scripted)
    service = FakeOpsService(group_id=ALLOWED_GROUP_ID)
    async with asgi_client(_build_app(service, [])) as client:
        resp = await client.post(
            f"{API_PREFIX}/fulfillments/reply-target-0001/confirm-not-delivered",
            headers=HDR_MANAGE,
        )
    assert resp.status_code == 200
    assert scripted.calls, "confirm-not-delivered 前未裁决"
    intents = [i for (_g, i) in scripted.calls]
    assert "technical_cleanup" in intents, "资源释放必须走 TECHNICAL_CLEANUP"
    assert "business" not in intents, "资源释放不得走 BUSINESS(发正文)"
    assert service.not_delivered_calls == ["reply-target-0001"]


async def test_resume_commitment_consults_fact_finalization(monkeypatch) -> None:
    """AC2: 承诺续跑按 FACT_FINALIZATION，只续承诺不重发回复。"""
    scripted = ScriptedAdjudicate("admitted")
    install_scripted(monkeypatch, scripted)
    service = FakeOpsService(group_id=ALLOWED_GROUP_ID)
    async with asgi_client(_build_app(service, [])) as client:
        resp = await client.post(
            f"{API_PREFIX}/fulfillments/reply-target-0001/commitments/favorability_adjustment/resume",
            headers=HDR_MANAGE,
        )
    assert resp.status_code == 200
    assert scripted.calls, "续跑前未裁决"
    intents = [i for (_g, i) in scripted.calls]
    assert "fact_finalization" in intents, "承诺续跑未按 FACT_FINALIZATION 裁决"
    assert service.resume_calls == [("reply-target-0001", "favorability_adjustment")]


async def test_list_restricted_group_not_leaked(monkeypatch) -> None:
    """AC3: 列表在正文读取前按最小化归属投影过滤，不泄漏受限群存在性/计数。"""
    scripted = ScriptedAdjudicate("restricted")
    install_scripted(monkeypatch, scripted)
    service = FakeOpsService(group_id=RESTRICTED_GROUP_ID)
    audit: list[Any] = []
    async with asgi_client(_build_app(service, audit)) as client:
        resp = await client.get(
            f"{API_PREFIX}/fulfillments?limit=50&offset=0",
            headers=HDR_READ,
        )
    assert resp.status_code == 200
    body = resp.json()
    leaked = [item for item in body["items"] if item["group_id"] == RESTRICTED_GROUP_ID]
    assert leaked == [], "受限群履约在最小投影前泄漏"
    assert body["total"] == 0, "受限群计数泄漏到 total"
    assert RESTRICTED_GROUP_ID not in resp.text, "受限群号泄漏进响应结构"
    assert scripted.calls, "列表归属过滤前未逐项裁决"


async def test_confirm_delivered_group_id_not_in_audit(monkeypatch) -> None:
    """AC5: 调用方提交的 group ID 只回显在批准响应字段，不进入审计/告警。"""
    scripted = ScriptedAdjudicate("admitted")
    install_scripted(monkeypatch, scripted)
    service = FakeOpsService(group_id=ALLOWED_GROUP_ID)
    audit: list[Any] = []
    async with asgi_client(_build_app(service, audit)) as client:
        resp = await client.post(
            f"{API_PREFIX}/fulfillments/reply-target-0001/confirm-delivered",
            headers=HDR_MANAGE,
            json={"platform_message_id": "platform-7"},
        )
    assert resp.status_code in (200, 201)
    # 确认送达响应不属已批准回显字段：group ID 不应在响应正文出现
    assert ALLOWED_GROUP_ID not in resp.text, "group ID 在非批准字段回显"
    import json

    audit_serialized = json.dumps(
        [event.to_dict() for event in audit],
        ensure_ascii=False,
    )
    assert ALLOWED_GROUP_ID not in audit_serialized, "group ID 泄漏进审计"
    assert "platform-7" not in audit_serialized, "平台消息 ID 泄漏进审计"
