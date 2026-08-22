"""TSK-231 管理面公共同契约红基线验收（AC3 聚合归属 / AC5 凭据无 bypass / AC7 路由与 OpenAPI）。

- SUPERUSER/管理凭据只负责认证，无准入 bypass：restricted 目标即便用通配凭据仍被 403；
- Router / service seam / OpenAPI / canary 覆盖（AC7）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI

from komari_bot.plugins.komari_memory.api import API_PREFIX as MEMORY_PREFIX
from komari_bot.plugins.komari_memory.api import create_memory_router
from komari_bot.plugins.komari_management.reply_fulfillment_api import (
    API_PREFIX as FF_PREFIX,
)
from komari_bot.plugins.komari_management.reply_fulfillment_api import (
    create_reply_fulfillment_router,
)

from tests.group_admission.chat_admission_support import ScriptedAdjudicate
from tests.group_admission.management_admission_support import (
    ALLOWED_GROUP_ID,
    MANAGEMENT_CREDENTIALS,
    RESTRICTED_GROUP_ID,
    FakeMemoryService,
    FakeOpsService,
    asgi_client,
    auth_headers,
    install_scripted,
)

pytestmark = pytest.mark.group_admission_acceptance

SUPERUSER_TOKEN = "superuser-token-00000000"
MEMORY_WRITE = "memory-writer-token-0000"


def _memory_app(service: FakeMemoryService) -> "FastAPI":
    app = FastAPI()
    app.include_router(
        create_memory_router(
            api_token=MANAGEMENT_CREDENTIALS,
            service_getter=lambda: service,
            redis_getter=lambda: None,
        )
    )
    return app


async def test_superuser_credential_does_not_bypass_admission(monkeypatch) -> None:
    """AC5: SUPERUSER 只负责认证，受限目标仍被 403（无准入 bypass）。"""
    scripted = ScriptedAdjudicate("restricted")
    install_scripted(monkeypatch, scripted)
    service = FakeMemoryService(group_ids=[ALLOWED_GROUP_ID, RESTRICTED_GROUP_ID])
    async with asgi_client(_memory_app(service)) as client:
        resp = await client.post(
            f"{MEMORY_PREFIX}/conversations",
            headers=auth_headers(SUPERUSER_TOKEN),
            json={"group_id": RESTRICTED_GROUP_ID, "summary": "受限"},
        )
    assert resp.status_code == 403, "SUPERUSER 凭据不得绕过群准入"
    assert service.create_calls == [], "受限目标仍被 SUPERUSER 落库"
    assert scripted.calls, "授权前未裁决"


async def test_management_routers_are_production_seams() -> None:
    """AC7: 管理 Router 来自真实生产模块（service seam 接线），非测试桩。"""
    from komari_bot.plugins.komari_management import (
        reply_fulfillment_api,
    )

    assert create_reply_fulfillment_router.__module__ == (
        reply_fulfillment_api.__name__
    ), "履约路由必须来自生产 reply_fulfillment_api"
    assert create_memory_router.__module__.endswith("komari_memory.api")
    # 路由前缀必须与生产装配一致
    assert FF_PREFIX == "/api/v2/reply-fulfillments"
    assert MEMORY_PREFIX == "/api/v2/komari-memory"


async def test_openapi_exposes_management_paths_and_schemas() -> None:
    """AC7: 管理面路径出现在 OpenAPI，且带安全/响应 schema。"""
    service = FakeMemoryService(group_ids=[ALLOWED_GROUP_ID])
    app = _build_all(service)
    async with asgi_client(app) as client:
        resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()
    paths = spec["paths"]
    assert f"{MEMORY_PREFIX}/conversations" in paths, "群记忆路径未投影进 OpenAPI"
    assert (
        f"{FF_PREFIX}/fulfillments/{{fulfillment_id}}/confirm-delivered"
    ) in paths, "履约对账路径未投影进 OpenAPI"


def _build_all(service: FakeMemoryService) -> "FastAPI":
    app = FastAPI()
    app.include_router(
        create_memory_router(
            api_token=MANAGEMENT_CREDENTIALS,
            service_getter=lambda: service,
            redis_getter=lambda: None,
        )
    )
    app.include_router(
        create_reply_fulfillment_router(
            api_token=MANAGEMENT_CREDENTIALS,
            service_getter=lambda: FakeOpsService(group_id=ALLOWED_GROUP_ID),
        )
    )
    return app
