"""Komari Management 维护通知接口路由测试。"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi import FastAPI

from komari_bot.onebot.onebot_messages import plain_text_message
from komari_bot.plugins.komari_management.announce_api import (
    API_PREFIX,
    register_announce_api,
)
from komari_bot.plugins.komari_management.announcement_repository import (
    InMemoryAnnouncementDispatchRepository,
)

if TYPE_CHECKING:
    from nonebug import App
    from pytest import MonkeyPatch

    from komari_bot.management.management_api import ManagementTokenSource
    from komari_bot.management.management_audit import ManagementAuditEvent

_DEFAULT_CREDENTIALS = (
    {
        "credential_id": "announce-operator",
        "token": "secret-token-00000000",
        "permissions": ["*"],
    },
)


class _FakeBot:
    def __init__(
        self,
        *,
        groups: list[dict[str, Any]] | None = None,
        fail_group_ids: set[int] | None = None,
        fail_group_list: bool = False,
        send_exceptions: dict[int, Exception] | None = None,
        send_started: asyncio.Event | None = None,
        send_release: asyncio.Event | None = None,
    ) -> None:
        self.groups = groups if groups is not None else [
            {
                "group_id": 10001,
                "group_name": "测试群",
                "member_count": 12,
            },
            {
                "group_id": 10002,
                "member_count": 8,
            },
        ]
        self.fail_group_ids = fail_group_ids or set()
        self.fail_group_list = fail_group_list
        self.send_exceptions = send_exceptions or {}
        self.send_started = send_started
        self.send_release = send_release
        self.sent_messages: list[dict[str, Any]] = []

    async def call_api(self, api: str, **kwargs: Any) -> Any:
        if api == "get_group_list":
            if self.fail_group_list:
                raise RuntimeError("群列表查询失败")
            return self.groups
        if api == "send_group_msg":
            self.sent_messages.append({"api": api, **kwargs})
            if self.send_started is not None:
                self.send_started.set()
            if self.send_release is not None:
                await self.send_release.wait()
            if error := self.send_exceptions.get(int(kwargs["group_id"])):
                raise error
            if kwargs["group_id"] in self.fail_group_ids:
                raise RuntimeError("发送失败")
            return {"message_id": 1}
        raise AssertionError


def _build_app(
    *,
    api_token: ManagementTokenSource = _DEFAULT_CREDENTIALS,
    status_page_url: str = "https://status.example.com/komari",
    announce_max_group_count: int = 20,
    announce_send_interval_seconds: float = 0.0,
    announce_request_cooldown_seconds: float = 0.0,
    audit_events: list[ManagementAuditEvent] | None = None,
    dispatch_repository: InMemoryAnnouncementDispatchRepository | None = None,
) -> FastAPI:
    async def _record_audit(event: ManagementAuditEvent) -> None:
        if audit_events is not None:
            audit_events.append(event)

    api_app = FastAPI()
    register_announce_api(
        api_app,
        api_token=api_token,
        allowed_origins=["https://ui.example.com"],
        status_page_url=status_page_url,
        announce_max_group_count=announce_max_group_count,
        announce_send_interval_seconds=announce_send_interval_seconds,
        announce_request_cooldown_seconds=announce_request_cooldown_seconds,
        audit_recorder=_record_audit,
        dispatch_repository=(
            dispatch_repository or InMemoryAnnouncementDispatchRepository()
        ),
    )
    return api_app


def _patch_bots(monkeypatch: MonkeyPatch, bots: dict[str, _FakeBot]) -> None:
    monkeypatch.setattr("nonebot.get_bots", lambda: bots)


def _write_headers(
    request_id: str = "announce-request",
    token: str = "secret-token-00000000",
) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Komari-Change-Reason": "验证维护公告",
        "X-Request-ID": request_id,
    }


@pytest.mark.asyncio
async def test_announce_routes_require_token_and_list_groups(
    app: App,
    monkeypatch: MonkeyPatch,
) -> None:
    _patch_bots(monkeypatch, {"bot": _FakeBot()})

    async with app.test_server(asgi=cast("Any", _build_app())) as ctx:
        client = ctx.get_client()
        unauthorized = await client.get(f"{API_PREFIX}/groups")
        listed = await client.get(
            f"{API_PREFIX}/groups",
            headers={"Authorization": "Bearer secret-token-00000000"},
        )

    assert unauthorized.status_code == 401
    assert listed.status_code == 200
    payload = listed.json()
    assert payload["total"] == 2
    assert payload["unavailable_bot_count"] == 0
    assert payload["groups"][0] == {
        "group_id": 10001,
        "group_name": "测试群",
        "member_count": 12,
        "bot_ids": ["bot"],
    }
    assert payload["groups"][1] == {
        "group_id": 10002,
        "group_name": "10002",
        "member_count": 8,
        "bot_ids": ["bot"],
    }


@pytest.mark.asyncio
async def test_announce_routes_support_group_send_and_failure_details(
    app: App,
    monkeypatch: MonkeyPatch,
) -> None:
    bot = _FakeBot(fail_group_ids={10002})
    _patch_bots(monkeypatch, {"bot": bot})
    audit_events: list[ManagementAuditEvent] = []

    async with app.test_server(
        asgi=cast("Any", _build_app(audit_events=audit_events))
    ) as ctx:
        client = ctx.get_client()
        response = await client.post(
            f"{API_PREFIX}/maintenance",
            headers=_write_headers("announce-partial-failure"),
            json={
                "title": "数据库维护",
                "content": "- 更新索引\n- 重启服务",
                "scheduled_time": "2026-04-24 02:00",
                "group_ids": [10001, 10002],
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert payload["success_count"] == 1
    assert payload["failed_count"] == 1
    assert payload["unreachable_count"] == 0
    assert payload["unavailable_bot_count"] == 0
    assert payload["results"] == [
        {
            "group_id": 10001,
            "success": True,
            "status": "success",
            "bot_id": "bot",
            "error_code": None,
            "error": None,
        },
        {
            "group_id": 10002,
            "success": False,
            "status": "failed",
            "bot_id": "bot",
            "error_code": "send_failed",
            "error": "所有候选 Bot 的平台发送接口均明确失败",
        },
    ]
    assert len(bot.sent_messages) == 2
    assert bot.sent_messages[0]["message"] == plain_text_message(
        "📢 预定维护通知\n\n"
        "【维护标题】\n"
        "数据库维护\n\n"
        "【维护内容】\n"
        "- 更新索引\n- 重启服务\n\n"
        "【预定维护时间】\n"
        "2026-04-24 02:00\n\n"
        "※ 实际的维护结束时间可能会提前或推迟\n"
        "※ 具体维护情况参考 Komari Bot Status 页面：\n"
        "   https://status.example.com/komari"
    )
    assert [event.outcome for event in audit_events] == ["started", "succeeded"]
    assert {event.operator_id for event in audit_events} == {"announce-operator"}
    assert audit_events[-1].metadata == {
        "target_count": 2,
        "success_count": 1,
        "failed_count": 1,
        "unreachable_count": 0,
        "unavailable_bot_count": 0,
        "idempotent_replay": False,
    }
    serialized_events = json.dumps(
        [event.to_dict() for event in audit_events],
        ensure_ascii=False,
    )
    assert "secret-token-00000000" not in serialized_events
    assert "数据库维护" not in serialized_events
    assert "10001" not in serialized_events


@pytest.mark.asyncio
async def test_announce_routes_handle_offline_bot(
    app: App, monkeypatch: MonkeyPatch
) -> None:
    _patch_bots(monkeypatch, {})
    read_headers = {"Authorization": "Bearer secret-token-00000000"}

    async with app.test_server(asgi=cast("Any", _build_app())) as ctx:
        client = ctx.get_client()
        groups = await client.get(f"{API_PREFIX}/groups", headers=read_headers)
        maintenance = await client.post(
            f"{API_PREFIX}/maintenance",
            headers=_write_headers("announce-offline"),
            json={
                "title": "数据库维护",
                "content": "- 更新索引",
                "scheduled_time": "2026-04-24 02:00",
                "group_ids": [10001],
            },
        )

    assert groups.status_code == 200
    assert groups.json() == {
        "groups": [],
        "total": 0,
        "unavailable_bot_count": 0,
    }
    assert maintenance.status_code == 503
    assert maintenance.json()["detail"] == "Bot 不在线，无法发送消息"


@pytest.mark.asyncio
async def test_announce_routes_reject_group_count_over_limit(
    app: App,
    monkeypatch: MonkeyPatch,
) -> None:
    bot = _FakeBot()
    _patch_bots(monkeypatch, {"bot": bot})

    async with app.test_server(
        asgi=cast("Any", _build_app(announce_max_group_count=1))
    ) as ctx:
        client = ctx.get_client()
        response = await client.post(
            f"{API_PREFIX}/maintenance",
            headers=_write_headers("announce-too-many"),
            json={
                "title": "数据库维护",
                "content": "- 更新索引",
                "scheduled_time": "2026-04-24 02:00",
                "group_ids": [10001, 10002],
            },
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "目标群数量超过上限 1"
    assert bot.sent_messages == []


@pytest.mark.asyncio
async def test_announce_routes_apply_request_cooldown(
    app: App,
    monkeypatch: MonkeyPatch,
) -> None:
    bot = _FakeBot()
    _patch_bots(monkeypatch, {"bot": bot})
    payload = {
        "title": "数据库维护",
        "content": "- 更新索引",
        "scheduled_time": "2026-04-24 02:00",
        "group_ids": [10001],
    }

    async with app.test_server(
        asgi=cast("Any", _build_app(announce_request_cooldown_seconds=60.0))
    ) as ctx:
        client = ctx.get_client()
        first = await client.post(
            f"{API_PREFIX}/maintenance",
            headers=_write_headers("announce-cooldown-first"),
            json=payload,
        )
        second = await client.post(
            f"{API_PREFIX}/maintenance",
            headers=_write_headers("announce-cooldown-second"),
            json=payload,
        )

    assert first.status_code == 200
    assert second.status_code == 429
    detail = second.json()["detail"]
    assert detail["message"] == "维护通知发送过于频繁，请稍后再试"
    assert 0 < detail["remaining_seconds"] <= 60.0
    assert len(bot.sent_messages) == 1


@pytest.mark.asyncio
async def test_announce_routes_throttle_between_group_sends(
    app: App,
    monkeypatch: MonkeyPatch,
) -> None:
    from komari_bot.plugins.komari_management import announce_api

    bot = _FakeBot(
        groups=[
            {"group_id": 10001, "member_count": 1},
            {"group_id": 10002, "member_count": 1},
            {"group_id": 10003, "member_count": 1},
        ],
        fail_group_ids={10002},
    )
    _patch_bots(monkeypatch, {"bot": bot})
    sleep_calls: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr(announce_api.asyncio, "sleep", _fake_sleep)

    async with app.test_server(
        asgi=cast("Any", _build_app(announce_send_interval_seconds=2.5))
    ) as ctx:
        client = ctx.get_client()
        response = await client.post(
            f"{API_PREFIX}/maintenance",
            headers=_write_headers("announce-throttle"),
            json={
                "title": "数据库维护",
                "content": "- 更新索引",
                "scheduled_time": "2026-04-24 02:00",
                "group_ids": [10001, 10002, 10003],
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    assert payload["success_count"] == 2
    assert payload["failed_count"] == 1
    assert payload["results"][1] == {
        "group_id": 10002,
        "success": False,
        "status": "failed",
        "bot_id": "bot",
        "error_code": "send_failed",
        "error": "所有候选 Bot 的平台发送接口均明确失败",
    }
    assert sleep_calls == [2.5, 2.5]
    assert len(bot.sent_messages) == 3


@pytest.mark.asyncio
async def test_announce_routes_use_the_bot_that_can_reach_each_group(
    app: App,
    monkeypatch: MonkeyPatch,
) -> None:
    bot_a = _FakeBot(
        groups=[
            {"group_id": 10001, "group_name": "A 群", "member_count": 10},
            {"group_id": 10003, "group_name": "共享群", "member_count": 20},
        ]
    )
    bot_b = _FakeBot(
        groups=[
            {"group_id": 10002, "group_name": "B 群", "member_count": 30},
            {"group_id": 10003, "group_name": "共享群", "member_count": 25},
        ]
    )
    unavailable_bot = _FakeBot(fail_group_list=True)
    _patch_bots(
        monkeypatch,
        {"bot-b": bot_b, "bot-a": bot_a, "bot-unavailable": unavailable_bot},
    )

    async with app.test_server(asgi=cast("Any", _build_app())) as ctx:
        client = ctx.get_client()
        listed = await client.get(
            f"{API_PREFIX}/groups",
            headers={"Authorization": "Bearer secret-token-00000000"},
        )
        sent = await client.post(
            f"{API_PREFIX}/maintenance",
            headers=_write_headers("announce-multi-bot"),
            json={
                "title": "分片路由验证",
                "content": "逐群发送",
                "scheduled_time": "2026-07-17 03:00",
                "group_ids": [10002, 99999, 10001, 10003],
            },
        )

    assert listed.status_code == 200
    assert listed.json()["unavailable_bot_count"] == 1
    assert listed.json()["groups"][-1] == {
        "group_id": 10003,
        "group_name": "共享群",
        "member_count": 25,
        "bot_ids": ["bot-a", "bot-b"],
    }
    assert sent.status_code == 200
    results = sent.json()["results"]
    assert [(item["group_id"], item["status"], item["bot_id"]) for item in results] == [
        (10002, "success", "bot-b"),
        (99999, "unreachable", None),
        (10001, "success", "bot-a"),
        (10003, "success", "bot-a"),
    ]
    assert sent.json()["unreachable_count"] == 1
    assert sent.json()["unavailable_bot_count"] == 1
    assert [item["group_id"] for item in bot_a.sent_messages] == [10001, 10003]
    assert [item["group_id"] for item in bot_b.sent_messages] == [10002]
    assert unavailable_bot.sent_messages == []


@pytest.mark.asyncio
async def test_announce_routes_enforce_named_credential_permissions(
    app: App,
    monkeypatch: MonkeyPatch,
) -> None:
    bot = _FakeBot(groups=[{"group_id": 10001, "member_count": 1}])
    _patch_bots(monkeypatch, {"bot": bot})
    credentials = [
        {
            "credential_id": "dashboard-reader",
            "token": "reader-token-00000000",
            "permissions": ["announce:read"],
        },
        {
            "credential_id": "release-operator",
            "token": "sender-token-00000000",
            "permissions": ["announce:send"],
        },
    ]
    payload = {
        "title": "权限验证",
        "content": "只允许发送凭据",
        "scheduled_time": "2026-07-17 03:00",
        "group_ids": [10001],
    }

    async with app.test_server(
        asgi=cast("Any", _build_app(api_token=credentials))
    ) as ctx:
        client = ctx.get_client()
        reader_list = await client.get(
            f"{API_PREFIX}/groups",
            headers={"Authorization": "Bearer reader-token-00000000"},
        )
        reader_send = await client.post(
            f"{API_PREFIX}/maintenance",
            headers=_write_headers("announce-reader-denied", "reader-token-00000000"),
            json=payload,
        )
        sender_send = await client.post(
            f"{API_PREFIX}/maintenance",
            headers=_write_headers("announce-sender", "sender-token-00000000"),
            json=payload,
        )

    assert reader_list.status_code == 200
    assert reader_send.status_code == 403
    assert sender_send.status_code == 200
    assert len(bot.sent_messages) == 1


@pytest.mark.asyncio
async def test_announce_routes_replay_completed_request_without_resending(
    app: App,
    monkeypatch: MonkeyPatch,
) -> None:
    bot = _FakeBot(groups=[{"group_id": 10001, "member_count": 1}])
    _patch_bots(monkeypatch, {"bot": bot})
    dispatches = InMemoryAnnouncementDispatchRepository()
    audit_events: list[ManagementAuditEvent] = []
    payload = {
        "title": "幂等验证",
        "content": "同一请求只能发送一次",
        "scheduled_time": "2026-07-17 03:00",
        "group_ids": [10001],
    }

    async with app.test_server(
        asgi=cast(
            "Any",
            _build_app(
                audit_events=audit_events,
                dispatch_repository=dispatches,
            ),
        )
    ) as ctx:
        client = ctx.get_client()
        first = await client.post(
            f"{API_PREFIX}/maintenance",
            headers=_write_headers("announce-idempotent"),
            json=payload,
        )
        replay = await client.post(
            f"{API_PREFIX}/maintenance",
            headers=_write_headers("announce-idempotent"),
            json=payload,
        )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert len(bot.sent_messages) == 1
    assert audit_events[-1].metadata == {"idempotent_replay": True}


@pytest.mark.asyncio
async def test_announce_routes_reject_request_id_payload_conflict(
    app: App,
    monkeypatch: MonkeyPatch,
) -> None:
    bot = _FakeBot(groups=[{"group_id": 10001, "member_count": 1}])
    _patch_bots(monkeypatch, {"bot": bot})
    dispatches = InMemoryAnnouncementDispatchRepository()
    payload = {
        "title": "原公告",
        "content": "内容",
        "scheduled_time": "2026-07-17 03:00",
        "group_ids": [10001],
    }

    async with app.test_server(
        asgi=cast(
            "Any",
            _build_app(dispatch_repository=dispatches),
        )
    ) as ctx:
        client = ctx.get_client()
        first = await client.post(
            f"{API_PREFIX}/maintenance",
            headers=_write_headers("announce-conflict"),
            json=payload,
        )
        conflicting = await client.post(
            f"{API_PREFIX}/maintenance",
            headers=_write_headers("announce-conflict"),
            json={**payload, "title": "被替换的公告"},
        )

    assert first.status_code == 200
    assert conflicting.status_code == 409
    assert conflicting.json()["detail"] == "同一 request ID 不允许绑定不同公告内容"
    assert len(bot.sent_messages) == 1


@pytest.mark.asyncio
async def test_announce_routes_fail_over_only_after_definite_failure(
    app: App,
    monkeypatch: MonkeyPatch,
) -> None:
    bot_a = _FakeBot(
        groups=[{"group_id": 10001, "member_count": 1}],
        fail_group_ids={10001},
    )
    bot_b = _FakeBot(groups=[{"group_id": 10001, "member_count": 1}])
    _patch_bots(monkeypatch, {"bot-a": bot_a, "bot-b": bot_b})

    async with app.test_server(asgi=cast("Any", _build_app())) as ctx:
        response = await ctx.get_client().post(
            f"{API_PREFIX}/maintenance",
            headers=_write_headers("announce-failover"),
            json={
                "title": "故障转移",
                "content": "验证候选 Bot",
                "scheduled_time": "2026-07-17 03:00",
                "group_ids": [10001],
            },
        )

    assert response.status_code == 200
    assert response.json()["results"][0]["bot_id"] == "bot-b"
    assert len(bot_a.sent_messages) == 1
    assert len(bot_b.sent_messages) == 1


@pytest.mark.asyncio
async def test_announce_routes_do_not_resend_unknown_delivery(
    app: App,
    monkeypatch: MonkeyPatch,
) -> None:
    bot_a = _FakeBot(
        groups=[{"group_id": 10001, "member_count": 1}],
        send_exceptions={10001: TimeoutError("平台可能已经接收")},
    )
    bot_b = _FakeBot(groups=[{"group_id": 10001, "member_count": 1}])
    _patch_bots(monkeypatch, {"bot-a": bot_a, "bot-b": bot_b})

    async with app.test_server(asgi=cast("Any", _build_app())) as ctx:
        response = await ctx.get_client().post(
            f"{API_PREFIX}/maintenance",
            headers=_write_headers("announce-delivery-unknown"),
            json={
                "title": "未知送达验证",
                "content": "不得重复发送",
                "scheduled_time": "2026-07-17 03:00",
                "group_ids": [10001],
            },
        )

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["error_code"] == "delivery_unknown"
    assert result["bot_id"] == "bot-a"
    assert len(bot_a.sent_messages) == 1
    assert bot_b.sent_messages == []


@pytest.mark.asyncio
async def test_announce_routes_reject_duplicate_and_over_budget_content(
    app: App,
    monkeypatch: MonkeyPatch,
) -> None:
    bot = _FakeBot(groups=[{"group_id": 10001, "member_count": 1}])
    _patch_bots(monkeypatch, {"bot": bot})
    base_payload = {
        "title": "预算验证",
        "content": "内容",
        "scheduled_time": "2026-07-17 03:00",
        "group_ids": [10001],
    }

    async with app.test_server(
        asgi=cast(
            "Any",
            _build_app(status_page_url="https://status.example/" + "x" * 4096),
        )
    ) as ctx:
        client = ctx.get_client()
        duplicate = await client.post(
            f"{API_PREFIX}/maintenance",
            headers=_write_headers("announce-duplicate-groups"),
            json={**base_payload, "group_ids": [10001, 10001]},
        )
        title_too_long = await client.post(
            f"{API_PREFIX}/maintenance",
            headers=_write_headers("announce-title-budget"),
            json={**base_payload, "title": "题" * 129},
        )
        message_too_long = await client.post(
            f"{API_PREFIX}/maintenance",
            headers=_write_headers("announce-message-budget"),
            json=base_payload,
        )

    assert duplicate.status_code == 422
    assert title_too_long.status_code == 422
    assert message_too_long.status_code == 422
    assert bot.sent_messages == []


@pytest.mark.asyncio
async def test_announce_routes_coordinate_concurrent_workers(
    app: App,
    monkeypatch: MonkeyPatch,
) -> None:
    send_started = asyncio.Event()
    send_release = asyncio.Event()
    bot = _FakeBot(
        groups=[{"group_id": 10001, "member_count": 1}],
        send_started=send_started,
        send_release=send_release,
    )
    _patch_bots(monkeypatch, {"bot": bot})
    dispatches = InMemoryAnnouncementDispatchRepository()
    payload = {
        "title": "并发验证",
        "content": "两个 worker 只能有一个发送者",
        "scheduled_time": "2026-07-17 03:00",
        "group_ids": [10001],
    }

    async with app.test_server(
        asgi=cast("Any", _build_app(dispatch_repository=dispatches))
    ) as ctx:
        client = ctx.get_client()
        first_task = asyncio.create_task(
            client.post(
                f"{API_PREFIX}/maintenance",
                headers=_write_headers("announce-concurrent"),
                json=payload,
            )
        )
        await asyncio.wait_for(send_started.wait(), timeout=1.0)
        concurrent = await client.post(
            f"{API_PREFIX}/maintenance",
            headers=_write_headers("announce-concurrent"),
            json=payload,
        )
        send_release.set()
        first = await first_task
        replay = await client.post(
            f"{API_PREFIX}/maintenance",
            headers=_write_headers("announce-concurrent"),
            json=payload,
        )

    assert first.status_code == 200
    assert concurrent.status_code == 409
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert len(bot.sent_messages) == 1


@pytest.mark.asyncio
async def test_announce_routes_release_unstarted_claim_when_bot_is_offline(
    app: App,
    monkeypatch: MonkeyPatch,
) -> None:
    dispatches = InMemoryAnnouncementDispatchRepository()
    payload = {
        "title": "离线恢复",
        "content": "上线后应允许安全重试",
        "scheduled_time": "2026-07-17 03:00",
        "group_ids": [10001],
    }
    _patch_bots(monkeypatch, {})

    async with app.test_server(
        asgi=cast("Any", _build_app(dispatch_repository=dispatches))
    ) as ctx:
        client = ctx.get_client()
        offline = await client.post(
            f"{API_PREFIX}/maintenance",
            headers=_write_headers("announce-offline-retry"),
            json=payload,
        )
        bot = _FakeBot(groups=[{"group_id": 10001, "member_count": 1}])
        _patch_bots(monkeypatch, {"bot": bot})
        retried = await client.post(
            f"{API_PREFIX}/maintenance",
            headers=_write_headers("announce-offline-retry"),
            json=payload,
        )

    assert offline.status_code == 503
    assert retried.status_code == 200
    assert len(bot.sent_messages) == 1
