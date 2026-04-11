"""ForgettingService tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as _SimpleNamespace
from types import SimpleNamespace
from typing import Any, cast

from komari_bot.plugins.komari_memory.services import (
    forgetting_service as forgetting_service_module,
)
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
        forgetting_fuzzify_concurrency=2,
        response_tag="content",
        llm_model_summary="summary-model",
        llm_temperature_summary=0.3,
        llm_max_tokens_summary=256,
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
    assert "importance_initial <= $1" in query
    assert "created_at <= NOW() - ($2 * INTERVAL '1 day')" in query
    assert args == (3, 7)


def test_daily_decay_uses_integer_step_down() -> None:
    conn = _FakeConnection(execute_results=["UPDATE 4"])
    service = _make_service(conn)

    asyncio.run(service._daily_decay())

    assert len(conn.execute_calls) == 1
    query, args = conn.execute_calls[0]
    assert "GREATEST(importance_current - 1, 0)" in query
    assert args == ()


def test_fuzzify_and_cleanup_high_value_memories_limits_concurrency() -> None:
    rows = [
        {"id": 11, "summary": "总结1"},
        {"id": 12, "summary": "总结2"},
        {"id": 13, "summary": "总结3"},
        {"id": 14, "summary": "总结4"},
    ]
    conn = _FakeConnection(
        execute_results=["DELETE 1"],
        fetch_rows=rows,
    )
    service = _make_service(conn)
    current_in_flight = 0
    max_in_flight = 0

    async def _fake_fuzzify(conv_id: int, original_summary: str) -> bool:
        del conv_id, original_summary
        nonlocal current_in_flight, max_in_flight
        current_in_flight += 1
        max_in_flight = max(max_in_flight, current_in_flight)
        await asyncio.sleep(0.01)
        current_in_flight -= 1
        return True

    service._fuzzify_conversation = _fake_fuzzify  # type: ignore[method-assign]

    total = asyncio.run(service._fuzzify_and_cleanup_high_value_memories())

    assert total == 5
    assert max_in_flight <= 2
    delete_query, delete_args = conn.execute_calls[0]
    fetch_query, fetch_args = conn.fetch_calls[0]
    assert "importance_initial > $1" in delete_query
    assert "is_fuzzy = TRUE" in delete_query
    assert delete_args == (3, 7)
    assert "importance_initial > $1" in fetch_query
    assert "is_fuzzy = FALSE" in fetch_query
    assert fetch_args == (3, 7)


def test_fuzzify_conversation_extracts_only_tag_content(monkeypatch: Any) -> None:
    conn = _FakeConnection(execute_results=["UPDATE 1"])
    service = _make_service(conn)
    llm_calls: list[dict[str, Any]] = []

    async def _fake_generate_text(**kwargs: Any) -> str:
        llm_calls.append(dict(kwargs))
        return "<think>略</think>\n<content>模糊后的结果</content>\n这里是多余废话"

    monkeypatch.setattr(
        forgetting_service_module,
        "llm_provider",
        _SimpleNamespace(generate_text=_fake_generate_text),
    )

    ok = asyncio.run(service._fuzzify_conversation(10, "原始总结内容"))

    assert ok is True
    assert llm_calls
    assert "标签外不要输出任何解释" in llm_calls[0]["prompt"]
    assert "<content>模糊化后的结果</content>" in llm_calls[0]["prompt"]
    update_query, update_args = conn.execute_calls[0]
    assert "is_fuzzy = TRUE" in update_query
    assert "importance_current = importance_initial" in update_query
    assert update_args == ("模糊后的结果", 10)
