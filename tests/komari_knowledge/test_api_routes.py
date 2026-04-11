"""Komari Knowledge API 路由测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI

from komari_bot.plugins.komari_knowledge.api import API_PREFIX, register_knowledge_api
from komari_bot.plugins.komari_knowledge.engine import UNSET, SearchResult
from komari_bot.plugins.komari_knowledge.models import KnowledgeCategory, KnowledgeEntry

if TYPE_CHECKING:
    from nonebug import App


def _with_query(path: str, **params: object) -> str:
    query = "&".join(
        f"{key}={value}" for key, value in params.items() if value is not None
    )
    return f"{path}?{query}" if query else path


def _build_entry(
    *,
    kid: int = 1,
    content: str = "小鞠喜欢布丁",
    keywords: list[str] | None = None,
    category: KnowledgeCategory = "character",
    notes: str | None = "初始备注",
) -> KnowledgeEntry:
    timestamp = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)
    return KnowledgeEntry(
        id=kid,
        category=category,
        keywords=keywords or ["小鞠", "布丁"],
        content=content,
        notes=notes,
        created_at=timestamp,
        updated_at=timestamp,
    )


class _FakeEngine:
    def __init__(self) -> None:
        self.entries = {
            1: _build_entry(),
            2: _build_entry(
                kid=2,
                content="小鞠讨厌苦瓜",
                keywords=["小鞠", "苦瓜"],
                category="general",
                notes=None,
            ),
        }
        self.list_calls: list[
            tuple[int, int, str | None, KnowledgeCategory | None]
        ] = []
        self.add_calls: list[
            tuple[str, list[str], KnowledgeCategory, str | None]
        ] = []
        self.update_calls: list[tuple[int, dict[str, object]]] = []
        self.delete_calls: list[int] = []
        self.search_calls: list[tuple[str, int | None]] = []

    async def list_knowledge(
        self,
        *,
        limit: int,
        offset: int,
        query: str | None = None,
        category: KnowledgeCategory | None = None,
    ) -> tuple[list[KnowledgeEntry], int]:
        self.list_calls.append((limit, offset, query, category))
        return [self.entries[1]], len(self.entries)

    async def get_knowledge(self, kid: int) -> KnowledgeEntry | None:
        return self.entries.get(kid)

    async def add_knowledge(
        self,
        content: str,
        keywords: list[str],
        category: KnowledgeCategory = "general",
        notes: str | None = None,
    ) -> int:
        self.add_calls.append((content, keywords, category, notes))
        kid = max(self.entries) + 1
        self.entries[kid] = _build_entry(
            kid=kid,
            content=content,
            keywords=keywords,
            category=category,
            notes=notes,
        )
        return kid

    async def update_knowledge(self, kid: int, **kwargs: object) -> bool:
        self.update_calls.append((kid, kwargs))
        entry = self.entries.get(kid)
        if entry is None:
            return False

        changes: dict[str, object] = {
            "updated_at": datetime(2026, 4, 10, 13, 0, tzinfo=UTC)
        }
        if kwargs.get("content", UNSET) is not UNSET:
            changes["content"] = kwargs["content"]
        if kwargs.get("keywords", UNSET) is not UNSET:
            changes["keywords"] = kwargs["keywords"]
        if kwargs.get("category", UNSET) is not UNSET:
            changes["category"] = kwargs["category"]
        if kwargs.get("notes", UNSET) is not UNSET:
            changes["notes"] = kwargs["notes"]

        self.entries[kid] = entry.model_copy(update=changes)
        return True

    async def delete_knowledge(self, kid: int) -> bool:
        self.delete_calls.append(kid)
        return self.entries.pop(kid, None) is not None

    async def search(
        self,
        query: str,
        limit: int | None = None,
        query_vec: list[float] | None = None,
    ) -> list[SearchResult]:
        del query_vec
        self.search_calls.append((query, limit))
        return [
            SearchResult(
                id=1,
                category="character",
                content="小鞠喜欢布丁",
                similarity=0.93,
                source="vector",
            )
        ]


def _build_app(engine: _FakeEngine | None) -> FastAPI:
    api_app = FastAPI()
    register_knowledge_api(
        api_app,
        api_token="secret-token",
        allowed_origins=["https://ui.example.com"],
        engine_getter=lambda: engine,
    )
    return api_app


@pytest.mark.asyncio
async def test_knowledge_routes_require_token_and_handle_cors(app: App) -> None:
    async with app.test_server(asgi=_build_app(_FakeEngine())) as ctx:
        client = ctx.get_client()

        unauthorized = await client.get(f"{API_PREFIX}/knowledge")
        assert unauthorized.status_code == 401

        wrong_token = await client.get(
            f"{API_PREFIX}/knowledge",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert wrong_token.status_code == 401

        preflight = await client.options(
            f"{API_PREFIX}/knowledge",
            headers={
                "Origin": "https://ui.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert preflight.status_code == 200
        assert (
            preflight.headers["access-control-allow-origin"]
            == "https://ui.example.com"
        )


@pytest.mark.asyncio
async def test_knowledge_routes_return_503_when_engine_unavailable(app: App) -> None:
    async with app.test_server(asgi=_build_app(None)) as ctx:
        client = ctx.get_client()
        response = await client.get(
            f"{API_PREFIX}/knowledge",
            headers={"Authorization": "Bearer secret-token"},
        )

    assert response.status_code == 503
    assert "引擎未初始化" in response.json()["detail"]


@pytest.mark.asyncio
async def test_list_and_get_knowledge_routes_forward_filters(app: App) -> None:
    engine = _FakeEngine()

    async with app.test_server(asgi=_build_app(engine)) as ctx:
        client = ctx.get_client()
        response = await client.get(
            _with_query(
                f"{API_PREFIX}/knowledge",
                q="布丁",
                category="character",
                limit=5,
                offset=3,
            ),
            headers={"Authorization": "Bearer secret-token"},
        )
        detail = await client.get(
            f"{API_PREFIX}/knowledge/1",
            headers={"Authorization": "Bearer secret-token"},
        )
        missing = await client.get(
            f"{API_PREFIX}/knowledge/999",
            headers={"Authorization": "Bearer secret-token"},
        )

    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert response.json()["items"][0]["content"] == "小鞠喜欢布丁"
    assert engine.list_calls == [(5, 3, "布丁", "character")]
    assert detail.status_code == 200
    assert detail.json()["id"] == 1
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_create_update_delete_and_search_routes(app: App) -> None:
    engine = _FakeEngine()
    headers = {"Authorization": "Bearer secret-token"}

    async with app.test_server(asgi=_build_app(engine)) as ctx:
        client = ctx.get_client()
        created = await client.post(
            f"{API_PREFIX}/knowledge",
            json={
                "content": "  小鞠喜欢打游戏  ",
                "keywords": ["小鞠", " 游戏 "],
                "category": "character",
                "notes": " 熬夜记录 ",
            },
            headers=headers,
        )
        updated = await client.patch(
            f"{API_PREFIX}/knowledge/1",
            json={"notes": None, "content": "小鞠超喜欢布丁"},
            headers=headers,
        )
        searched = await client.post(
            f"{API_PREFIX}/search",
            json={"query": "布丁", "limit": 2},
            headers=headers,
        )
        deleted = await client.delete(f"{API_PREFIX}/knowledge/2", headers=headers)
        missing_delete = await client.delete(
            f"{API_PREFIX}/knowledge/999",
            headers=headers,
        )

    assert created.status_code == 201
    assert created.json()["id"] == 3
    assert engine.add_calls[0] == (
        "小鞠喜欢打游戏",
        ["小鞠", "游戏"],
        "character",
        "熬夜记录",
    )
    assert updated.status_code == 200
    assert updated.json()["notes"] is None
    assert engine.update_calls[0][0] == 1
    assert searched.status_code == 200
    assert searched.json()[0]["similarity"] == 0.93
    assert engine.search_calls == [("布丁", 2)]
    assert deleted.status_code == 204
    assert missing_delete.status_code == 404


@pytest.mark.asyncio
async def test_update_validation_errors_are_reported(app: App) -> None:
    headers = {"Authorization": "Bearer secret-token"}

    async with app.test_server(asgi=_build_app(_FakeEngine())) as ctx:
        client = ctx.get_client()
        empty_patch = await client.patch(
            f"{API_PREFIX}/knowledge/1",
            json={},
            headers=headers,
        )
        missing_content = await client.patch(
            f"{API_PREFIX}/knowledge/1",
            json={"content": None},
            headers=headers,
        )

    assert empty_patch.status_code == 422
    assert "至少提供一个要更新的字段" in empty_patch.json()["detail"]
    assert missing_content.status_code == 422
    assert "content 不能为空" in missing_content.json()["detail"]
