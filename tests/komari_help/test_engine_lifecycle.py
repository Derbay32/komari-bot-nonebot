"""HelpEngine 生命周期与索引快照测试。"""

from __future__ import annotations

import asyncio
from typing import Any, Self

from komari_bot.plugins.komari_help import engine as engine_module
from komari_bot.plugins.komari_help.engine import HelpEngine, state


class _FakePool:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.version = 0
        self.closed = False

    def acquire(self) -> Self:
        return self

    def transaction(self, **kwargs: object) -> Self:
        assert kwargs == {"isolation": "repeatable_read", "readonly": True}
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb

    async def fetchval(self, query: str, index_name: str) -> int:
        assert "komari_search_index_versions" in query
        assert index_name == "komari_help"
        return self.version

    async def fetch(self, query: str) -> list[dict[str, Any]]:
        assert "SELECT id, title, plugin_name, keywords" in query
        return self.rows

    async def close(self) -> None:
        self.closed = True


def test_help_index_rebuild_atomically_replaces_stale_snapshot() -> None:
    engine = HelpEngine()
    pool = _FakePool(
        rows=[
            {
                "id": 1,
                "title": "旧帮助",
                "plugin_name": "old_plugin",
                "keywords": ["旧词"],
            }
        ]
    )
    engine._pool = pool
    asyncio.run(engine._build_keyword_index())
    previous_snapshot = engine._keyword_index.snapshot

    pool.rows = [
        {
            "id": 2,
            "title": "新帮助",
            "plugin_name": "new_plugin",
            "keywords": ["新词"],
        }
    ]
    pool.version = 1
    asyncio.run(engine._build_keyword_index())

    assert "旧词" in previous_snapshot.entries
    assert "旧词" not in engine._keyword_index.entries
    assert engine._keyword_index.entries["新词"] == frozenset({2})
    assert engine._keyword_index.entries["new_plugin"] == frozenset({2})


def test_help_close_resets_index_and_pool() -> None:
    engine = HelpEngine()
    pool = _FakePool(
        rows=[
            {
                "id": 1,
                "title": "帮助",
                "plugin_name": "demo",
                "keywords": ["帮助"],
            }
        ]
    )
    engine._pool = pool
    asyncio.run(engine._build_keyword_index())
    engine._initialized = True

    asyncio.run(engine.close())

    assert pool.closed is True
    assert engine._pool is None
    assert engine._keyword_index.entries == {}
    assert engine._keyword_index.loaded is False
    assert engine._initialized is False


def test_help_initialize_engine_is_single_flight(monkeypatch: Any) -> None:
    original_engine = state.engine
    initialize_calls = 0

    async def _record_initialize(self: HelpEngine) -> None:
        nonlocal initialize_calls
        del self
        initialize_calls += 1
        await asyncio.sleep(0)

    async def _run_concurrently() -> tuple[HelpEngine, HelpEngine]:
        first, second = await asyncio.gather(
            engine_module.initialize_engine(),
            engine_module.initialize_engine(),
        )
        return first, second

    monkeypatch.setattr(engine_module.HelpEngine, "initialize", _record_initialize)
    state.engine = None
    try:
        first, second = asyncio.run(_run_concurrently())
        assert first is second
        assert initialize_calls == 1
    finally:
        state.engine = original_engine


def test_help_engine_instance_initialize_is_single_flight(monkeypatch: Any) -> None:
    engine = HelpEngine()
    initialize_calls = 0

    async def _initialize_once() -> None:
        nonlocal initialize_calls
        initialize_calls += 1
        await asyncio.sleep(0)
        engine._initialized = True

    async def _run_concurrently() -> None:
        await asyncio.gather(engine.initialize(), engine.initialize())

    monkeypatch.setattr(engine, "_initialize_once", _initialize_once)

    asyncio.run(_run_concurrently())

    assert initialize_calls == 1
