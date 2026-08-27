"""TSK-231 管理面维护公告 (announce) 目标准入红基线验收。

驱动真实 ``register_announce_api`` + ``InMemoryAnnouncementDispatchRepository`` +
内存 fake Bot，安装脚本化 ``adjudicate``。逐 AC 断言：

- 单目标受限 -> 403，不发送；
- 批量逐目标返回结果，拒绝目标不阻塞获准目标、不计业务失败/重试（AC4）；
- 相同 request/payload 幂等回放不重发；delivery unknown 永不重发（AC6）。

当前生产公告端点在逐目标发送前不裁决，故受限态用例以红态失败证明接入缺失。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI

from komari_bot.plugins.komari_management.announce_api import (
    API_PREFIX,
    register_announce_api,
)
from komari_bot.plugins.komari_management.announcement_repository import (
    InMemoryAnnouncementDispatchRepository,
)
from tests.group_admission.chat_admission_support import ScriptedAdjudicate
from tests.group_admission.management_admission_support import (
    ALLOWED_GROUP_ID,
    MANAGEMENT_CREDENTIALS,
    RESTRICTED_GROUP_ID,
    FakeBot,
    asgi_client,
    auth_headers,
    install_scripted,
)

pytestmark = pytest.mark.group_admission_acceptance

ANNOUNCE_OPERATOR = "announce-send-token-000000"
AUDIT: list[Any] = []


def _build_app(repo: InMemoryAnnouncementDispatchRepository) -> "FastAPI":
    async def _record(event: Any) -> None:
        AUDIT.append(event)

    app = FastAPI()
    register_announce_api(
        app,
        api_token=MANAGEMENT_CREDENTIALS,
        allowed_origins=["https://ui.example.com"],
        status_page_url="https://status.example.com/komari",
        announce_send_interval_seconds=0.0,
        announce_request_cooldown_seconds=0.0,
        audit_recorder=_record,
        dispatch_repository=repo,
    )
    return app


def _headers(request_id: str) -> dict[str, str]:
    return auth_headers(ANNOUNCE_OPERATOR, request_id=request_id)


def _payload(group_ids: list[int]) -> dict[str, Any]:
    return {
        "title": "数据库维护",
        "content": "- 更新索引",
        "scheduled_time": "2026-04-24 02:00",
        "group_ids": group_ids,
    }


async def test_announce_single_restricted_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC1 single-restricted: 公告单目标受限 -> 403，不发送。"""
    scripted = ScriptedAdjudicate("restricted")
    install_scripted(monkeypatch, scripted)
    bot = FakeBot()
    monkeypatch.setattr("nonebot.get_bots", lambda: {"bot": bot})
    repo = InMemoryAnnouncementDispatchRepository()
    AUDIT.clear()

    async with asgi_client(_build_app(repo)) as client:
        resp = await client.post(
            f"{API_PREFIX}/maintenance",
            headers=_headers("announce-restricted-single"),
            json=_payload([int(RESTRICTED_GROUP_ID)]),
        )
    assert resp.status_code == 403, "受限目标公告必须被拒绝"
    assert bot.sent_messages == [], "受限目标仍发送了公告"
    assert scripted.calls, "公告发送前未裁决"


async def test_announce_batch_restricted_not_blocking_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC4: 批量逐目标裁决；受限目标不阻塞合法目标、不计业务失败/重试。"""
    scripted = ScriptedAdjudicate("admitted")
    scripted.set_sequence("admitted", "restricted")
    install_scripted(monkeypatch, scripted)
    bot = FakeBot()
    monkeypatch.setattr("nonebot.get_bots", lambda: {"bot": bot})
    repo = InMemoryAnnouncementDispatchRepository()
    AUDIT.clear()

    async with asgi_client(_build_app(repo)) as client:
        resp = await client.post(
            f"{API_PREFIX}/maintenance",
            headers=_headers("announce-batch"),
            json=_payload([int(ALLOWED_GROUP_ID), int(RESTRICTED_GROUP_ID)]),
        )
    assert resp.status_code == 200
    body = resp.json()
    sent_ids = [int(msg.get("group_id") or 0) for msg in bot.sent_messages]
    assert int(ALLOWED_GROUP_ID) in sent_ids, "合法目标应被发送"
    assert int(RESTRICTED_GROUP_ID) not in sent_ids, "受限目标仍被发送"
    result_ids = {item["group_id"] for item in body["results"]}
    assert int(RESTRICTED_GROUP_ID) not in result_ids, "受限目标泄漏进结果集"
    assert body["success_count"] == 1, "受限目标不应计为业务成功"
    assert body["failed_count"] == 0, "受限目标不应计为业务失败/重试"
    assert len(scripted.calls) == 2, "每个目标各需一次裁决"


async def test_announce_replay_same_payload_never_resends(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC6: 相同 request/payload 幂等回放不重发；adjudicate 只裁决一次发送。"""
    scripted = ScriptedAdjudicate("admitted")
    install_scripted(monkeypatch, scripted)
    bot = FakeBot()
    monkeypatch.setattr("nonebot.get_bots", lambda: {"bot": bot})
    repo = InMemoryAnnouncementDispatchRepository()
    AUDIT.clear()

    async with asgi_client(_build_app(repo)) as client:
        first = await client.post(
            f"{API_PREFIX}/maintenance",
            headers=_headers("announce-idem"),
            json=_payload([int(ALLOWED_GROUP_ID)]),
        )
        replay = await client.post(
            f"{API_PREFIX}/maintenance",
            headers=_headers("announce-idem"),
            json=_payload([int(ALLOWED_GROUP_ID)]),
        )
    assert first.status_code == 200 and replay.status_code == 200
    assert replay.json() == first.json(), "幂等回放应返回存储结果"
    assert len(bot.sent_messages) == 1, "幂等回放不得重发"


async def test_announce_delivery_unknown_never_resends(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC6: delivery unknown 永不重发；受限/unknown 终态不随恢复复活。"""
    scripted = ScriptedAdjudicate("admitted")
    install_scripted(monkeypatch, scripted)

    async def _fail_delivery(api: str, **kwargs: Any) -> Any:
        del kwargs
        if api == "get_group_list":
            return [{"group_id": int(ALLOWED_GROUP_ID), "group_name": "可获准群", "member_count": 1}]
        if api == "send_group_msg":
            raise TimeoutError("平台可能已接收")  # delivery unknown
        raise AssertionError

    bot = FakeBot(groups=[{"group_id": int(ALLOWED_GROUP_ID), "member_name": "x", "member_count": 1}])
    monkeypatch.setattr("nonebot.get_bots", lambda: {"bot": bot})
    bot.call_api = _fail_delivery  # type: ignore[method-assign]
    repo = InMemoryAnnouncementDispatchRepository()
    AUDIT.clear()

    async with asgi_client(_build_app(repo)) as client:
        resp = await client.post(
            f"{API_PREFIX}/maintenance",
            headers=_headers("announce-unknown"),
            json=_payload([int(ALLOWED_GROUP_ID)]),
        )
    assert resp.status_code == 200
    result = resp.json()["results"][0]
    assert result["error_code"] == "delivery_unknown", "送达结果未知应分类为 delivery_unknown"
    assert bot.sent_messages == [], "unknown 终态不得再次补发（同一请求内）"
    assert scripted.calls, "送达路径前未裁决"
