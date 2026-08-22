"""TSK-231 管理面 dead-letter 目标准入红基线验收。

驱动真实 ``create_memory_router`` 的 dead-letter 路由（list / requeue）+ 注入式
``FakeDeadLetterManager``，安装脚本化 ``adjudicate``。逐 AC 断言：

- 查看/发送类读正文为 BUSINESS intent，正文读取前按最小归属投影过滤；
- 列表不泄漏受限群存在性（受限群号/计数不得出现在响应）（AC3）；
- 单目标 requeue/replay 在受限态下整条拒绝 -> 403（AC1），且不经业务重试路径。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI

from komari_bot.plugins.komari_memory.api import API_PREFIX, create_memory_router

from tests.group_admission.chat_admission_support import ScriptedAdjudicate
from tests.group_admission.management_admission_support import (
    ALLOWED_GROUP_ID,
    MANAGEMENT_CREDENTIALS,
    RESTRICTED_GROUP_ID,
    FakeDeadLetterManager,
    asgi_client,
    auth_headers,
    install_scripted,
)

pytestmark = pytest.mark.group_admission_acceptance

MEMORY_READER = "memory-reader-token-0000"
MEMORY_WRITE = "memory-writer-token-0000"


def _build_app(manager: FakeDeadLetterManager) -> "FastAPI":
    app = FastAPI()
    app.include_router(
        create_memory_router(
            api_token=MANAGEMENT_CREDENTIALS,
            service_getter=lambda: None,  # 本文件只测 dead-letter 面
            redis_getter=lambda: manager,
        )
    )
    return app


def _admitted_intents(scripted):
    return [intent for (_g, intent) in scripted.calls]


async def test_dead_letter_list_restricted_not_leaked(monkeypatch) -> None:
    """AC3: 摘要读取为 BUSINESS 且受限群不进入列表/计数。"""
    scripted = ScriptedAdjudicate("restricted")
    install_scripted(monkeypatch, scripted)
    manager = FakeDeadLetterManager(
        group_ids=[ALLOWED_GROUP_ID, RESTRICTED_GROUP_ID]
    )
    async with asgi_client(_build_app(manager)) as client:
        resp = await client.get(
            f"{API_PREFIX}/conversation-dead-letters?limit=50",
            headers=auth_headers(MEMORY_READER),
        )
    assert resp.status_code == 200
    body = resp.json()
    leaked = [
        item for item in body["items"] if item["group_id"] == RESTRICTED_GROUP_ID
    ]
    assert leaked == [], "受限群 dead-letter 泄漏进列表"
    assert RESTRICTED_GROUP_ID not in resp.text, "受限群号泄漏进响应结构"
    # 读正文为 BUSINESS 裁决：实现对受限群最小归属读取，只返回全量摘要仍红。
    assert scripted.calls, "dead-letter 读取前未裁决"


async def test_dead_letter_list_consults_business_intent(monkeypatch) -> None:
    """AC5: dead-letter 读取正文为 BUSINESS 分类。"""
    scripted = ScriptedAdjudicate("admitted")
    install_scripted(monkeypatch, scripted)
    manager = FakeDeadLetterManager(group_ids=[ALLOWED_GROUP_ID])
    async with asgi_client(_build_app(manager)) as client:
        resp = await client.get(
            f"{API_PREFIX}/conversation-dead-letters?limit=50",
            headers=auth_headers(MEMORY_READER),
        )
    assert resp.status_code == 200
    assert scripted.calls, "读取前未裁决"
    intents = _admitted_intents(scripted)
    assert "business" in intents, "dead-letter 读取未按 BUSINESS 裁决"


async def test_dead_letter_requeue_restricted_forbidden(monkeypatch) -> None:
    """AC5 single-restricted: requeue 受限群 -> 403，不清除/重放快照。"""
    scripted = ScriptedAdjudicate("restricted")
    install_scripted(monkeypatch, scripted)
    manager = FakeDeadLetterManager(group_ids=[ALLOWED_GROUP_ID, RESTRICTED_GROUP_ID])
    async with asgi_client(_build_app(manager)) as client:
        resp = await client.post(
            f"{API_PREFIX}/conversation-dead-letters/"
            f"{RESTRICTED_GROUP_ID}/snap-10002/requeue",
            headers=auth_headers(MEMORY_READER),
        )
    assert resp.status_code in (403, 404), "受限群 requeue 必须被拒绝或视为不可达"
    assert manager.requeue_calls == [], "受限群仍触发了 requeue 重放"
    assert scripted.calls, "requeue 重放前未裁决"
