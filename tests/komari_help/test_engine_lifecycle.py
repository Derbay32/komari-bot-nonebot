"""HelpEngine 生命周期与索引快照测试。"""

from __future__ import annotations

import asyncio
from typing import Any

from komari_bot.plugins.komari_help import engine as engine_module
from komari_bot.plugins.komari_help.engine import HelpEngine, state


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
