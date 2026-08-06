"""KnowledgeEngine lifecycle tests."""

from __future__ import annotations

import asyncio
from typing import Any

from komari_bot.plugins.komari_knowledge import engine as engine_module
from komari_bot.plugins.komari_knowledge.engine import KnowledgeEngine, state


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


def test_initialize_engine_is_single_flight(monkeypatch: Any) -> None:
    original_engine = state.engine
    initialize_calls = 0

    async def _record_initialize(self: KnowledgeEngine) -> None:
        nonlocal initialize_calls
        del self
        initialize_calls += 1
        await asyncio.sleep(0)

    async def _run_concurrently() -> tuple[KnowledgeEngine, KnowledgeEngine]:
        first, second = await asyncio.gather(
            engine_module.initialize_engine(),
            engine_module.initialize_engine(),
        )
        return first, second

    monkeypatch.setattr(engine_module.KnowledgeEngine, "initialize", _record_initialize)
    state.engine = None
    try:
        first, second = asyncio.run(_run_concurrently())
        assert first is second
        assert initialize_calls == 1
    finally:
        state.engine = original_engine


def test_engine_instance_initialize_is_single_flight(monkeypatch: Any) -> None:
    engine = KnowledgeEngine()
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
