"""ForgettingService tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from komari_bot.plugins.komari_memory.services.forgetting_service import (
    ForgettingService,
)


class _FakeConnection:
    def __init__(
        self,
        *,
        execute_results: list[str] | None = None,
        fetch_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.execute_results = list(execute_results or [])
        self.fetch_rows = list(fetch_rows or [])
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, query: str, *args: object) -> str:
        self.execute_calls.append((query, args))
        if self.execute_results:
            return self.execute_results.pop(0)
        return "DELETE 0"

    async def fetch(self, query: str, *args: object) -> list[dict[str, Any]]:
        self.fetch_calls.append((query, args))
        return list(self.fetch_rows)


class _FakeAcquire:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConnection:
        return self._conn

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb


class _FakePool:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._conn)


def _make_service(conn: _FakeConnection) -> ForgettingService:
    config = SimpleNamespace(
        forgetting_enabled=True,
        forgetting_importance_threshold=3,
        forgetting_min_age_days=7,
        forgetting_decay_factor=0.95,
        llm_model_summary="summary-model",
    )
    return ForgettingService(
        config=cast("Any", config),
        pg_pool=cast("Any", _FakePool(conn)),
    )


def test_delete_low_value_memories_respects_min_age_days() -> None:
    conn = _FakeConnection(execute_results=["DELETE 2"])
    service = _make_service(conn)

    deleted = asyncio.run(service._delete_low_value_memories())

    assert deleted == 2
    assert len(conn.execute_calls) == 1
    query, args = conn.execute_calls[0]
    assert "created_at <= NOW() - ($2 * INTERVAL '1 day')" in query
    assert args == (3, 7)


def test_fuzzify_and_cleanup_high_value_memories_respects_min_age_days() -> None:
    conn = _FakeConnection(
        execute_results=["DELETE 1"],
        fetch_rows=[{"id": 10, "summary": "foo"}],
    )
    service = _make_service(conn)
    fuzzified_ids: list[int] = []

    async def _fake_fuzzify(conv_id: int, original_summary: str) -> None:
        del original_summary
        fuzzified_ids.append(conv_id)

    service._fuzzify_conversation = _fake_fuzzify  # type: ignore[method-assign]

    total = asyncio.run(service._fuzzify_and_cleanup_high_value_memories())

    assert total == 2
    delete_query, delete_args = conn.execute_calls[0]
    fetch_query, fetch_args = conn.fetch_calls[0]
    assert "created_at <= NOW() - ($2 * INTERVAL '1 day')" in delete_query
    assert delete_args == (3, 7)
    assert "created_at <= NOW() - ($2 * INTERVAL '1 day')" in fetch_query
    assert fetch_args == (3, 7)
    assert fuzzified_ids == [10]
