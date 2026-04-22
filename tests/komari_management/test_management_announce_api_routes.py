"""Komari Management 维护通知接口路由测试。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi import FastAPI

from komari_bot.plugins.komari_management.announce_api import (
    API_PREFIX,
    register_announce_api,
)

if TYPE_CHECKING:
    from nonebug import App
    from pytest import MonkeyPatch


class _FakeBot:
    def __init__(self, *, fail_group_ids: set[int] | None = None) -> None:
        self.fail_group_ids = fail_group_ids or set()
        self.sent_messages: list[dict[str, Any]] = []

    async def call_api(self, api: str, **kwargs: Any) -> Any:
        if api == "get_group_list":
            return [
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
        if api == "send_group_msg":
            self.sent_messages.append({"api": api, **kwargs})
            if kwargs["group_id"] in self.fail_group_ids:
                raise RuntimeError("发送失败")
            return {"message_id": 1}
        raise AssertionError


def _build_app() -> FastAPI:
    api_app = FastAPI()
    register_announce_api(
        api_app,
        api_token="secret-token",
        allowed_origins=["https://ui.example.com"],
        status_page_url="https://status.example.com/komari",
    )
    return api_app


def _patch_bots(monkeypatch: MonkeyPatch, bots: dict[str, _FakeBot]) -> None:
    monkeypatch.setattr("nonebot.get_bots", lambda: bots)


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
            headers={"Authorization": "Bearer secret-token"},
        )

    assert unauthorized.status_code == 401
    assert listed.status_code == 200
    payload = listed.json()
    assert payload["total"] == 2
    assert payload["groups"][0] == {
        "group_id": 10001,
        "group_name": "测试群",
        "member_count": 12,
    }
    assert payload["groups"][1] == {
        "group_id": 10002,
        "group_name": "10002",
        "member_count": 8,
    }


@pytest.mark.asyncio
async def test_announce_routes_support_group_send_and_failure_details(
    app: App,
    monkeypatch: MonkeyPatch,
) -> None:
    bot = _FakeBot(fail_group_ids={10002})
    _patch_bots(monkeypatch, {"bot": bot})
    headers = {"Authorization": "Bearer secret-token"}

    async with app.test_server(asgi=cast("Any", _build_app())) as ctx:
        client = ctx.get_client()
        response = await client.post(
            f"{API_PREFIX}/maintenance",
            headers=headers,
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
    assert payload["results"] == [
        {"group_id": 10001, "success": True, "error": None},
        {"group_id": 10002, "success": False, "error": "发送失败"},
    ]
    assert len(bot.sent_messages) == 2
    assert bot.sent_messages[0]["message"] == (
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


@pytest.mark.asyncio
async def test_announce_routes_handle_offline_bot(
    app: App, monkeypatch: MonkeyPatch
) -> None:
    _patch_bots(monkeypatch, {})
    headers = {"Authorization": "Bearer secret-token"}

    async with app.test_server(asgi=cast("Any", _build_app())) as ctx:
        client = ctx.get_client()
        groups = await client.get(f"{API_PREFIX}/groups", headers=headers)
        maintenance = await client.post(
            f"{API_PREFIX}/maintenance",
            headers=headers,
            json={
                "title": "数据库维护",
                "content": "- 更新索引",
                "scheduled_time": "2026-04-24 02:00",
                "group_ids": [10001],
            },
        )

    assert groups.status_code == 200
    assert groups.json() == {"groups": [], "total": 0}
    assert maintenance.status_code == 503
    assert maintenance.json()["detail"] == "Bot 不在线，无法发送消息"
