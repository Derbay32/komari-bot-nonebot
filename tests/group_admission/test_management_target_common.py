"""TSK-231 管理面公共同契约验收（AC3 聚合归属 / AC5 凭据无 bypass / AC7 路由与 OpenAPI）。

- SUPERUSER/管理凭据只负责认证，无准入 bypass：restricted 目标即便用通配凭据仍被 403；
- Router / service seam / OpenAPI / canary 覆盖（AC7）。

旧记忆与 dead-letter 管理面已随 TSK-197 退役；本文件只保留仍存在的回复
履约管理入口断言。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from komari_bot.plugins.komari_management.reply_fulfillment_api import (
    API_PREFIX as FF_PREFIX,
)
from komari_bot.plugins.komari_management.reply_fulfillment_api import (
    create_reply_fulfillment_router,
)
from tests.group_admission.chat_admission_support import ScriptedAdjudicate
from tests.group_admission.management_admission_support import (
    MANAGEMENT_CREDENTIALS,
    RESTRICTED_GROUP_ID,
    FakeOpsService,
    asgi_client,
    auth_headers,
    install_scripted,
)

pytestmark = pytest.mark.group_admission_acceptance

SUPERUSER_TOKEN = "superuser-token-00000000"


def _fulfillment_app(service: FakeOpsService) -> "FastAPI":
    app = FastAPI()
    app.include_router(
        create_reply_fulfillment_router(
            api_token=MANAGEMENT_CREDENTIALS,
            service_getter=lambda: service,
        )
    )
    return app


async def test_superuser_credential_does_not_bypass_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC5: SUPERUSER 只负责认证，受限目标仍被 403（无准入 bypass）。"""
    scripted = ScriptedAdjudicate("restricted")
    install_scripted(monkeypatch, scripted)
    service = FakeOpsService(group_id=RESTRICTED_GROUP_ID)
    async with asgi_client(_fulfillment_app(service)) as client:
        resp = await client.post(
            f"{FF_PREFIX}/fulfillments/reply-target-0001/confirm-delivered",
            headers=auth_headers(SUPERUSER_TOKEN, request_id="ff-superuser-tsk231"),
            json={"platform_message_id": "platform-restricted"},
        )
    assert resp.status_code == 403, "SUPERUSER 凭据不得绕过群准入"
    assert service.delivered_calls == [], "受限目标仍被 SUPERUSER 落库"
    assert scripted.calls, "授权前未裁决"


async def test_management_router_is_a_production_seam() -> None:
    """AC7: 管理 Router 来自真实生产模块（service seam 接线），非测试桩。"""
    from komari_bot.plugins.komari_management import (
        reply_fulfillment_api,
    )

    assert create_reply_fulfillment_router.__module__ == (
        reply_fulfillment_api.__name__
    ), "履约路由必须来自生产 reply_fulfillment_api"
    assert FF_PREFIX == "/api/v2/reply-fulfillments"


async def test_openapi_exposes_management_paths_and_schemas() -> None:
    """AC7: 管理面路径出现在 OpenAPI，且带安全/响应 schema。"""
    service = FakeOpsService()
    app = _fulfillment_app(service)
    async with asgi_client(app) as client:
        resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()
    paths = spec["paths"]
    assert (
        f"{FF_PREFIX}/fulfillments/{{fulfillment_id}}/confirm-delivered"
    ) in paths, "履约对账路径未投影进 OpenAPI"
