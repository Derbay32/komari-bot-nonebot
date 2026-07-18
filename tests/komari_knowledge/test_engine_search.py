"""KnowledgeEngine 检索行为测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from komari_bot.plugins.komari_knowledge import engine as engine_module
from komari_bot.plugins.komari_knowledge.engine import KnowledgeEngine
from komari_bot.plugins.komari_knowledge.models import SearchResult


class _FakeSearchPool:
    def __init__(self) -> None:
        self.fetch_calls: list[tuple[str, tuple[object, ...]]] = []

    def acquire(self) -> "_FakeSearchPool":
        return self

    async def __aenter__(self) -> "_FakeSearchPool":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb

    async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
        self.fetch_calls.append((query, args))
        return []


def _patch_config(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        engine_module,
        "get_config",
        lambda: SimpleNamespace(
            total_limit=5,
            layer1_limit=3,
            layer2_limit=2,
            similarity_threshold=0.0,
            query_rewrite_rules={"你": "小鞠", "您的": "小鞠的"},
        ),
    )


def test_search_rebuilds_embedding_when_rewrite_changes_query(
    monkeypatch: Any,
) -> None:
    _patch_config(monkeypatch)
    engine = KnowledgeEngine()
    pool = _FakeSearchPool()
    engine._pool = pool

    async def _fake_keyword_search(query: str, limit: int) -> list[object]:
        assert query == "小鞠喜欢什么"
        assert limit == 2
        return []

    captured_queries: list[str] = []

    async def _fake_get_embedding(query: str) -> list[float]:
        captured_queries.append(query)
        return [9.0, 8.0]

    monkeypatch.setattr(engine, "_layer1_keyword_search", _fake_keyword_search)
    monkeypatch.setattr(engine, "_get_embedding", _fake_get_embedding)

    asyncio.run(engine.search("你喜欢什么", limit=2, query_vec=[1.0, 2.0]))

    assert captured_queries == ["小鞠喜欢什么"]
    assert pool.fetch_calls[0][1][0] == str([9.0, 8.0])


def test_search_reuses_embedding_when_rewrite_does_not_change_query(
    monkeypatch: Any,
) -> None:
    _patch_config(monkeypatch)
    engine = KnowledgeEngine()
    pool = _FakeSearchPool()
    engine._pool = pool

    async def _fake_keyword_search(query: str, limit: int) -> list[object]:
        assert query == "小鞠喜欢什么"
        assert limit == 2
        return []

    async def _unexpected_get_embedding(_query: str) -> list[float]:
        raise AssertionError

    monkeypatch.setattr(engine, "_layer1_keyword_search", _fake_keyword_search)
    monkeypatch.setattr(engine, "_get_embedding", _unexpected_get_embedding)

    asyncio.run(engine.search("小鞠喜欢什么", limit=2, query_vec=[1.0, 2.0]))

    assert pool.fetch_calls[0][1][0] == str([1.0, 2.0])


def test_search_applies_independent_layer_limits(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        engine_module,
        "get_config",
        lambda: SimpleNamespace(
            total_limit=5,
            layer1_limit=1,
            layer2_limit=2,
            similarity_threshold=0.0,
            query_rewrite_rules={},
        ),
    )
    engine = KnowledgeEngine()
    engine._pool = _FakeSearchPool()
    calls: list[tuple[str, int]] = []

    async def _fake_keyword_search(
        _query: str,
        limit: int,
    ) -> list[SearchResult]:
        calls.append(("keyword", limit))
        return [
            SearchResult(
                id=1,
                category="general",
                content="关键词结果",
                similarity=1.0,
                source="keyword",
            )
        ]

    async def _fake_vector_search(
        _query: str,
        limit: int,
        _exclude_ids: set[int],
        query_vec: list[float] | None = None,
    ) -> list[SearchResult]:
        del query_vec
        calls.append(("vector", limit))
        return [
            SearchResult(
                id=index + 2,
                category="general",
                content=f"向量结果 {index}",
                similarity=0.9 - index / 10,
                source="vector",
            )
            for index in range(limit)
        ]

    monkeypatch.setattr(engine, "_layer1_keyword_search", _fake_keyword_search)
    monkeypatch.setattr(engine, "_layer2_vector_search", _fake_vector_search)

    results = asyncio.run(engine.search("测试", limit=5, query_vec=[1.0]))

    assert calls == [("keyword", 1), ("vector", 2)]
    assert [item.source for item in results] == ["keyword", "vector", "vector"]


def test_search_skips_disabled_layers(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        engine_module,
        "get_config",
        lambda: SimpleNamespace(
            total_limit=5,
            layer1_limit=0,
            layer2_limit=0,
            similarity_threshold=0.0,
            query_rewrite_rules={},
        ),
    )
    engine = KnowledgeEngine()
    engine._pool = _FakeSearchPool()

    async def _unexpected(*_args: object, **_kwargs: object) -> list[SearchResult]:
        raise AssertionError("关闭的分层检索不应被调用")

    monkeypatch.setattr(engine, "_layer1_keyword_search", _unexpected)
    monkeypatch.setattr(engine, "_layer2_vector_search", _unexpected)

    assert asyncio.run(engine.search("测试", limit=5, query_vec=[1.0])) == []
