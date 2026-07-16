"""KnowledgeEngine 管理接口相关测试。"""

from __future__ import annotations

import asyncio

import pytest

from komari_bot.common.content_budget import ContentValidationError
from komari_bot.plugins.komari_knowledge.engine import KnowledgeEngine


class _FakeListPool:
    def __init__(self) -> None:
        self.fetchval_calls: list[tuple[str, tuple[object, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[object, ...]]] = []

    def acquire(self) -> "_FakeListPool":
        return self

    async def __aenter__(self) -> "_FakeListPool":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb

    async def fetchval(self, query: str, *args: object) -> int:
        self.fetchval_calls.append((query, args))
        return 2

    async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
        self.fetch_calls.append((query, args))
        return [
            {
                "id": 11,
                "category": "character",
                "keywords": ["小鞠", "布丁"],
                "content": "小鞠喜欢布丁",
                "notes": "测试数据",
                "created_at": "2026-04-10T12:00:00+00:00",
                "updated_at": "2026-04-10T12:00:00+00:00",
            }
        ]


class _FakeUpdatePool:
    def __init__(self) -> None:
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []

    def acquire(self) -> "_FakeUpdatePool":
        return self

    async def __aenter__(self) -> "_FakeUpdatePool":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb

    async def fetchrow(self, query: str, *args: object) -> dict[str, object] | None:
        assert "SELECT content, keywords, category, notes" in query
        assert args == (1,)
        return {
            "content": "旧内容",
            "keywords": ["alpha", "beta"],
            "category": "general",
            "notes": "旧备注",
        }

    async def execute(self, query: str, *args: object) -> None:
        self.execute_calls.append((query, args))


class _FakeAddPool:
    def __init__(self) -> None:
        self.fetchval_calls: list[tuple[str, tuple[object, ...]]] = []

    def acquire(self) -> "_FakeAddPool":
        return self

    async def __aenter__(self) -> "_FakeAddPool":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb

    async def fetchval(self, query: str, *args: object) -> int:
        self.fetchval_calls.append((query, args))
        return 42


def test_list_knowledge_supports_filters_and_pagination() -> None:
    engine = KnowledgeEngine()
    pool = _FakeListPool()
    engine._pool = pool

    items, total = asyncio.run(
        engine.list_knowledge(
            limit=10,
            offset=5,
            query="布丁",
            category="character",
        )
    )

    count_query, count_args = pool.fetchval_calls[0]
    data_query, data_args = pool.fetch_calls[0]

    assert total == 2
    assert items[0].id == 11
    assert items[0].keywords == ["小鞠", "布丁"]
    assert "COUNT(*)" in count_query
    assert "unnest" in count_query
    assert count_args == ("%布丁%", "character")
    assert "ORDER BY created_at DESC" in data_query
    assert data_args == ("%布丁%", "character", 10, 5)


def test_list_knowledge_escapes_like_wildcards() -> None:
    engine = KnowledgeEngine()
    pool = _FakeListPool()
    engine._pool = pool

    asyncio.run(
        engine.list_knowledge(
            limit=10,
            offset=0,
            query=r"100%_x\tag",
        )
    )

    count_query, count_args = pool.fetchval_calls[0]
    _data_query, data_args = pool.fetch_calls[0]

    assert "content ILIKE $1 ESCAPE '\\'" in count_query
    assert "keyword ILIKE $1 ESCAPE '\\'" in count_query
    assert count_args == (r"%100\%\_x\\tag%",)
    assert data_args == (r"%100\%\_x\\tag%", 10, 0)


def test_update_knowledge_allows_clearing_notes_without_touching_embedding() -> None:
    engine = KnowledgeEngine()
    pool = _FakeUpdatePool()
    engine._pool = pool

    async def _unexpected_get_embedding(_text: str) -> list[float]:
        raise AssertionError

    engine._get_embedding = _unexpected_get_embedding  # type: ignore[method-assign]
    rebuild_calls = 0

    async def _rebuild_index() -> None:
        nonlocal rebuild_calls
        rebuild_calls += 1

    engine._build_keyword_index = _rebuild_index  # type: ignore[method-assign]

    updated = asyncio.run(engine.update_knowledge(1, notes=None))

    assert updated is True
    update_query, update_args = pool.execute_calls[0]
    assert "notes = $2" in update_query
    assert update_args == (1, None)
    assert rebuild_calls == 1


def test_add_knowledge_uses_source_key_for_idempotent_insert() -> None:
    engine = KnowledgeEngine()
    pool = _FakeAddPool()
    engine._pool = pool

    async def _get_embedding(_text: str) -> list[float]:
        return [0.1, 0.2]

    engine._get_embedding = _get_embedding  # type: ignore[method-assign]
    rebuild_calls = 0

    async def _rebuild_index() -> None:
        nonlocal rebuild_calls
        rebuild_calls += 1

    engine._build_keyword_index = _rebuild_index  # type: ignore[method-assign]

    knowledge_id = asyncio.run(
        engine.add_knowledge(
            "提案内容",
            ["提案"],
            "custom",
            source_key="komari_custom:proposal:9",
        )
    )

    query, args = pool.fetchval_calls[0]
    assert knowledge_id == 42
    assert "ON CONFLICT (source_key) WHERE source_key IS NOT NULL" in query
    assert args[-1] == "komari_custom:proposal:9"
    assert rebuild_calls == 1


def test_add_knowledge_direct_call_rejects_budget_before_embedding() -> None:
    engine = KnowledgeEngine()
    engine._pool = object()
    embedding_called = False

    async def _unexpected_embedding(_text: str) -> list[float]:
        nonlocal embedding_called
        embedding_called = True
        return []

    engine._get_embedding = _unexpected_embedding  # type: ignore[method-assign]

    with pytest.raises(ContentValidationError, match="估算 token 上限"):
        asyncio.run(engine.add_knowledge("测" * 6_001, ["测试"]))

    assert embedding_called is False
