"""KnowledgeEngine lifecycle tests."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from komari_bot.plugins.komari_knowledge import engine as engine_module
from komari_bot.plugins.komari_knowledge.engine import KnowledgeEngine, state


class _FakeEmbeddingService:
    def __init__(self) -> None:
        self.cleaned = False

    async def cleanup(self) -> None:
        self.cleaned = True


class _FakePool:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.closed = False

    def acquire(self) -> "_FakePool":
        return self

    async def __aenter__(self) -> "_FakePool":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb

    async def fetch(self, query: str) -> list[dict[str, Any]]:
        assert "SELECT id, keywords" in query
        return self.rows

    async def close(self) -> None:
        self.closed = True


def test_build_keyword_index_rebuild_clears_stale_entries() -> None:
    engine = KnowledgeEngine()
    engine._pool = _FakePool(
        rows=[
            {"id": 2, "keywords": ["Fresh", "Alpha"]},
            {"id": 3, "keywords": ["Alpha"]},
        ]
    )
    engine._keyword_index = defaultdict(set, {"stale": {1}, "alpha": {99}})
    engine._index_loaded = True

    asyncio.run(engine._build_keyword_index())

    assert "stale" not in engine._keyword_index
    assert engine._keyword_index["fresh"] == {2}
    assert engine._keyword_index["alpha"] == {2, 3}
    assert engine._index_loaded is True


def test_close_cleans_embedding_service_pool_and_global_state() -> None:
    engine = KnowledgeEngine()
    engine._pool = _FakePool()
    engine._embedding_service = _FakeEmbeddingService()
    engine._keyword_index = defaultdict(set, {"alpha": {1}})
    engine._index_loaded = True
    original_engine = state.engine
    state.engine = engine

    try:
        asyncio.run(engine.close())
    finally:
        if state.engine is engine:
            state.engine = original_engine

    assert engine._pool is None
    assert engine._embedding_service is None
    assert engine._keyword_index == {}
    assert engine._index_loaded is False
    assert state.engine is None

    state.engine = original_engine


def test_initialize_engine_does_not_keep_failed_instance(monkeypatch: Any) -> None:
    original_engine = state.engine

    async def _raise_initialize(self: KnowledgeEngine) -> None:
        del self
        raise RuntimeError("boom")

    monkeypatch.setattr(engine_module.KnowledgeEngine, "initialize", _raise_initialize)
    state.engine = None

    try:
        try:
            asyncio.run(engine_module.initialize_engine())
        except RuntimeError as exc:
            assert str(exc) == "boom"
        else:
            raise AssertionError
        assert state.engine is None
    finally:
        state.engine = original_engine
