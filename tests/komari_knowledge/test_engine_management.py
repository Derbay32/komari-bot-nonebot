"""KnowledgeEngine 管理接口相关测试。"""

from __future__ import annotations

import asyncio

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


def test_update_knowledge_allows_clearing_notes_without_touching_embedding() -> None:
    engine = KnowledgeEngine()
    pool = _FakeUpdatePool()
    engine._pool = pool

    async def _unexpected_get_embedding(_text: str) -> list[float]:
        raise AssertionError

    engine._get_embedding = _unexpected_get_embedding  # type: ignore[method-assign]

    updated = asyncio.run(engine.update_knowledge(1, notes=None))

    assert updated is True
    update_query, update_args = pool.execute_calls[0]
    assert "notes = $2" in update_query
    assert update_args == (1, None)
