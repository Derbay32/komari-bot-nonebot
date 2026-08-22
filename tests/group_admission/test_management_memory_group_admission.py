"""TSK-231 管理面群记忆（komari-memory 单目标）准入红基线验收。

驱动真实 ``create_memory_router`` 的 conversation-create 单目标效果 + 注入式
``FakeMemoryService``，安装脚本化 ``adjudicate``，覆盖 AC1 五态：

- admitted 执行 -> 201，且裁决发生在持久写之前；
- policy restricted -> 403，且不落库（create 不被调用）；
- effective policy unavailable -> 503；
- 服务端归属失败（group_attribution_unavailable）-> 503 且不执行；
- 请求缺字段 -> 422（生产 pydantic 契约，本项已满足，作为对照断言）。

当前生产单目标写前不调用 adjudicate，故受限/失败态用例红态失败。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from komari_bot.plugins.group_admission.contracts import (
    AdmissionQualification,
    AdmissionResult,
)
from komari_bot.plugins.komari_memory.api import API_PREFIX, create_memory_router
from tests.group_admission.chat_admission_support import ScriptedAdjudicate
from tests.group_admission.management_admission_support import (
    ALLOWED_GROUP_ID,
    MANAGEMENT_CREDENTIALS,
    RESTRICTED_GROUP_ID,
    FakeMemoryService,
    asgi_client,
    auth_headers,
    install_scripted,
)

pytestmark = pytest.mark.group_admission_acceptance

MEMORY_READER = "memory-reader-token-0000"
MEMORY_WRITE = "memory-writer-token-0000"


def _build_app(service: FakeMemoryService) -> "FastAPI":
    app = FastAPI()
    app.include_router(
        create_memory_router(
            api_token=MANAGEMENT_CREDENTIALS,
            service_getter=lambda: service,  # type: ignore[arg-type] -- 测试用 fake 注入
            redis_getter=lambda: None,
        )
    )
    return app


def _create_headers() -> dict[str, str]:
    return auth_headers(MEMORY_WRITE)


def _admitted_intents(scripted: ScriptedAdjudicate) -> list[object]:
    return [intent for (_g, intent) in scripted.calls]


async def test_create_conversation_admitted_executes(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC1 admitted: 单目标获准执行，且裁决在落库前以 BUSINESS。"""
    scripted = ScriptedAdjudicate("admitted")
    install_scripted(monkeypatch, scripted)
    service = FakeMemoryService(group_ids=[ALLOWED_GROUP_ID, RESTRICTED_GROUP_ID])
    async with asgi_client(_build_app(service)) as client:
        resp = await client.post(
            f"{API_PREFIX}/conversations",
            headers=_create_headers(),
            json={"group_id": ALLOWED_GROUP_ID, "summary": "新对话"},
        )
    assert resp.status_code == 201
    assert scripted.calls, "创建前未裁决"
    assert "business" in _admitted_intents(scripted), "单目标记忆写未按 BUSINESS 裁决"
    assert service.create_calls, "获准态应允许落库"


async def test_create_conversation_restricted_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC1 restricted: 受限群 -> 403，不下游落库。"""
    scripted = ScriptedAdjudicate("restricted")
    install_scripted(monkeypatch, scripted)
    service = FakeMemoryService(group_ids=[ALLOWED_GROUP_ID, RESTRICTED_GROUP_ID])
    async with asgi_client(_build_app(service)) as client:
        resp = await client.post(
            f"{API_PREFIX}/conversations",
            headers=_create_headers(),
            json={"group_id": RESTRICTED_GROUP_ID, "summary": "受限对话"},
        )
    assert resp.status_code == 403, "受限群单目标写必须拒绝"
    assert service.create_calls == [], "受限群仍落库"
    assert scripted.calls, "单目标写前未裁决"


async def test_create_conversation_policy_unavailable_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC1: effective policy unavailable -> 503，且不落库。"""
    scripted = ScriptedAdjudicate("failed")
    install_scripted(monkeypatch, scripted)
    service = FakeMemoryService(group_ids=[ALLOWED_GROUP_ID])
    async with asgi_client(_build_app(service)) as client:
        resp = await client.post(
            f"{API_PREFIX}/conversations",
            headers=_create_headers(),
            json={"group_id": ALLOWED_GROUP_ID, "summary": "策略不可用"},
        )
    assert resp.status_code == 503, "策略不可用应返回 503"
    assert service.create_calls == [], "策略不可用仍落库"
    assert scripted.calls, "主权化前未裁决"


async def test_create_conversation_attribution_failure_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC1: 服务端归属失败（group_attribution_unavailable）-> 503，不执行。"""
    scripted = ScriptedAdjudicate("admitted")
    install_scripted(monkeypatch, scripted)

    def _attribution_failed(
        groups: object, *, intent: str = "business", **_kwargs: object
    ) -> AdmissionResult:
        del groups, intent
        return AdmissionResult(
            qualification=AdmissionQualification.REJECTED,
            effective_revision=1,
            reason_code="group_attribution_unavailable",
        )

    # 直接覆盖包顶层 adjudicate：实例 __call__ 赋值不参与特殊方法查表，无效。
    import komari_bot.plugins.group_admission as admission_package

    monkeypatch.setattr(admission_package, "adjudicate", _attribution_failed)
    service = FakeMemoryService(group_ids=[ALLOWED_GROUP_ID])
    async with asgi_client(_build_app(service)) as client:
        resp = await client.post(
            f"{API_PREFIX}/conversations",
            headers=_create_headers(),
            json={"group_id": ALLOWED_GROUP_ID, "summary": "归属失败"},
        )
    assert resp.status_code == 503, "归属失败应返回 503"
    assert service.create_calls == [], "归属失败仍落库"


async def test_create_conversation_missing_field_422() -> None:
    """AC1 422: 请求缺必填字段 -> 422（pydantic 契约，当前已满足）。"""
    service = FakeMemoryService(group_ids=[ALLOWED_GROUP_ID])
    async with asgi_client(_build_app(service)) as client:
        resp = await client.post(
            f"{API_PREFIX}/conversations",
            headers=_create_headers(),
            json={"summary": "缺少 group_id"},
        )
    assert resp.status_code == 422, "缺必填字段应返回 422"
    assert service.create_calls == [], "缺字段不应落库"

