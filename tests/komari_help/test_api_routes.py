"""Komari Help API 路由测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi import FastAPI

from komari_bot.plugins.komari_help.api import API_PREFIX, register_help_api
from komari_bot.plugins.komari_help.engine import UNSET
from komari_bot.plugins.komari_help.models import HelpEntry, HelpSearchResult

if TYPE_CHECKING:
    from nonebug import App


def _build_entry(
    *,
    hid: int = 1,
    title: str = "角色绑定",
    content: str = "/bind set <角色名> — 设置角色绑定",
    plugin_name: str | None = "character_binding",
) -> HelpEntry:
    timestamp = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)
    return HelpEntry(
        id=hid,
        category="command",
        plugin_name=plugin_name,
        keywords=["绑定", "角色"],
        title=title,
        content=content,
        notes="备注",
        is_auto_generated=False,
        created_at=timestamp,
        updated_at=timestamp,
    )


class _FakeEngine:
    def __init__(self) -> None:
        self.entries = {
            1: _build_entry(),
            2: _build_entry(hid=2, title="今日好感", plugin_name="jrhg"),
        }
        self.list_calls: list[tuple[int, int, str | None, str | None]] = []
        self.add_calls: list[
            tuple[str, str, list[str], str, str | None, str | None]
        ] = []
        self.update_calls: list[tuple[int, dict[str, object]]] = []
        self.delete_calls: list[int] = []
        self.search_calls: list[tuple[str, int | None]] = []

    async def list_help(
        self,
        *,
        limit: int,
        offset: int,
        query: str | None = None,
        category: str | None = None,
    ) -> tuple[list[HelpEntry], int]:
        self.list_calls.append((limit, offset, query, category))
        return [self.entries[1]], len(self.entries)

    async def get_help(self, hid: int) -> HelpEntry | None:
        return self.entries.get(hid)

    async def add_help(
        self,
        title: str,
        content: str,
        keywords: list[str],
        category: str = "other",
        plugin_name: str | None = None,
        notes: str | None = None,
        *,
        is_auto_generated: bool = False,
    ) -> int:
        del is_auto_generated
        self.add_calls.append((title, content, keywords, category, plugin_name, notes))
        hid = max(self.entries) + 1
        self.entries[hid] = _build_entry(
            hid=hid,
            title=title,
            content=content,
            plugin_name=plugin_name,
        ).model_copy(
            update={"category": category, "keywords": keywords, "notes": notes}
        )
        return hid

    async def update_help(self, hid: int, **kwargs: object) -> bool:
        self.update_calls.append((hid, kwargs))
        entry = self.entries.get(hid)
        if entry is None:
            return False
        changes: dict[str, object] = {
            "updated_at": datetime(2026, 4, 10, 13, 0, tzinfo=UTC),
        }
        for field in [
            "title",
            "content",
            "keywords",
            "category",
            "plugin_name",
            "notes",
        ]:
            if kwargs.get(field, UNSET) is not UNSET:
                changes[field] = kwargs[field]
        self.entries[hid] = entry.model_copy(update=changes)
        return True

    async def delete_help(self, hid: int) -> bool:
        self.delete_calls.append(hid)
        return self.entries.pop(hid, None) is not None

    async def search(
        self,
        query: str,
        limit: int | None = None,
        query_vec: list[float] | None = None,
    ) -> list[HelpSearchResult]:
        del query_vec
        self.search_calls.append((query, limit))
        return [
            HelpSearchResult(
                id=1,
                category="command",
                plugin_name="character_binding",
                title="角色绑定",
                content="/bind set <角色名> — 设置角色绑定",
                similarity=0.92,
                source="vector",
            )
        ]


def _build_app(engine: _FakeEngine | None) -> FastAPI:
    api_app = FastAPI()
    register_help_api(
        api_app,
        api_token="secret-token",
        allowed_origins=["https://ui.example.com"],
        engine_getter=lambda: engine,
    )
    return api_app


@pytest.mark.asyncio
async def test_help_routes_require_token_and_handle_cors(app: App) -> None:
    async with app.test_server(asgi=cast("Any", _build_app(_FakeEngine()))) as ctx:
        client = ctx.get_client()
        unauthorized = await client.get(f"{API_PREFIX}/help")
        assert unauthorized.status_code == 401

        preflight = await client.options(
            f"{API_PREFIX}/help",
            headers={
                "Origin": "https://ui.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert preflight.status_code == 200
        assert (
            preflight.headers["access-control-allow-origin"] == "https://ui.example.com"
        )


@pytest.mark.asyncio
async def test_help_routes_list_get_create_update_delete_and_search(app: App) -> None:
    engine = _FakeEngine()
    headers = {"Authorization": "Bearer secret-token"}

    async with app.test_server(asgi=cast("Any", _build_app(engine))) as ctx:
        client = ctx.get_client()
        listed = await client.get(
            f"{API_PREFIX}/help?q=绑定&category=command&limit=5&offset=2",
            headers=headers,
        )
        detail = await client.get(f"{API_PREFIX}/help/1", headers=headers)
        created = await client.post(
            f"{API_PREFIX}/help",
            json={
                "title": "  角色绑定说明  ",
                "content": "  /bind help — 查看详细帮助  ",
                "keywords": [" 绑定 ", " 角色 "],
                "category": "command",
                "plugin_name": " character_binding ",
                "notes": "  自动整理  ",
            },
            headers=headers,
        )
        updated = await client.patch(
            f"{API_PREFIX}/help/1",
            json={"notes": None, "title": "新的角色绑定说明"},
            headers=headers,
        )
        searched = await client.post(
            f"{API_PREFIX}/search",
            json={"query": "绑定角色", "limit": 2},
            headers=headers,
        )
        deleted = await client.delete(f"{API_PREFIX}/help/2", headers=headers)

    assert listed.status_code == 200
    assert listed.json()["total"] == 2
    assert engine.list_calls == [(5, 2, "绑定", "command")]
    assert detail.status_code == 200
    assert created.status_code == 201
    assert engine.add_calls[0] == (
        "角色绑定说明",
        "/bind help — 查看详细帮助",
        ["绑定", "角色"],
        "command",
        "character_binding",
        "自动整理",
    )
    assert updated.status_code == 200
    assert updated.json()["title"] == "新的角色绑定说明"
    assert searched.status_code == 200
    assert searched.json()[0]["similarity"] == 0.92
    assert engine.search_calls == [("绑定角色", 2)]
    assert deleted.status_code == 204


@pytest.mark.asyncio
async def test_help_routes_return_503_when_engine_unavailable(app: App) -> None:
    async with app.test_server(asgi=cast("Any", _build_app(None))) as ctx:
        client = ctx.get_client()
        response = await client.get(
            f"{API_PREFIX}/help",
            headers={"Authorization": "Bearer secret-token"},
        )

    assert response.status_code == 503
    assert "引擎未初始化" in response.json()["detail"]
