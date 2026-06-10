"""ForgettingService tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from types import SimpleNamespace as _SimpleNamespace
from typing import Any, cast

from komari_bot.plugins.komari_memory.core import retry as retry_module
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


def test_fuzzify_and_cleanup_high_value_memories_continues_after_task_error() -> None:
    rows = [
        {"id": 21, "summary": "第一条"},
        {"id": 22, "summary": "第二条"},
        {"id": 23, "summary": "第三条"},
    ]
    conn = _FakeConnection(fetch_rows=rows)
    service = _make_service(conn)

    async def _fake_fuzzify(conv_id: int, original_summary: str) -> bool:
        del original_summary
        if conv_id == 22:
            raise RuntimeError("单条模糊化失败")
        return True

    service._fuzzify_conversation = _fake_fuzzify  # type: ignore[method-assign]

    total = asyncio.run(service._fuzzify_and_cleanup_high_value_memories())

    assert total == 2


def test_fuzzify_and_cleanup_high_value_memories_skips_bad_records() -> None:
    rows = [
        {"id": 31, "summary": "正常记录"},
        {"id": "bad", "summary": "坏 ID"},
        {"id": 33, "summary": None},
    ]
    conn = _FakeConnection(fetch_rows=rows)
    service = _make_service(conn)
    fuzzified_ids: list[int] = []

    async def _fake_fuzzify(conv_id: int, original_summary: str) -> bool:
        fuzzified_ids.append(conv_id)
        assert original_summary == "正常记录"
        return True

    service._fuzzify_conversation = _fake_fuzzify  # type: ignore[method-assign]

    total = asyncio.run(service._fuzzify_and_cleanup_high_value_memories())

    assert total == 1
    assert fuzzified_ids == [31]


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


def test_fuzzify_conversation_deletes_after_placeholder_retries(monkeypatch: Any) -> None:
    conn = _FakeConnection(execute_results=["DELETE 1"])
    service = _make_service(conn)
    llm_calls = 0

    async def _fake_sleep(_delay: float) -> None:
        return None

    async def _fake_generate_text(**_kwargs: Any) -> str:
        nonlocal llm_calls
        llm_calls += 1
        return "<content>对话内容已模糊化处理</content>"

    monkeypatch.setattr(retry_module.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(
        forgetting_service_module,
        "llm_provider",
        _SimpleNamespace(generate_text=_fake_generate_text),
    )

    ok = asyncio.run(service._fuzzify_conversation(20, "原始总结内容"))

    assert ok is True
    assert llm_calls == 3
    assert len(conn.execute_calls) == 1
    delete_query, delete_args = conn.execute_calls[0]
    assert "DELETE FROM komari_memory_conversations" in delete_query
    assert "WHERE id = $1" in delete_query
    assert delete_args == (20,)


def test_fuzzify_conversation_deletes_after_empty_retries(monkeypatch: Any) -> None:
    conn = _FakeConnection(execute_results=["DELETE 1"])
    service = _make_service(conn)
    llm_calls = 0

    async def _fake_sleep(_delay: float) -> None:
        return None

    async def _fake_generate_text(**_kwargs: Any) -> str:
        nonlocal llm_calls
        llm_calls += 1
        return "<content></content>"

    monkeypatch.setattr(retry_module.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(
        forgetting_service_module,
        "llm_provider",
        _SimpleNamespace(generate_text=_fake_generate_text),
    )

    ok = asyncio.run(service._fuzzify_conversation(21, "原始总结内容"))

    assert ok is True
    assert llm_calls == 3
    delete_query, delete_args = conn.execute_calls[0]
    assert "DELETE FROM komari_memory_conversations" in delete_query
    assert delete_args == (21,)


def test_fuzzify_conversation_updates_when_third_retry_is_valid(monkeypatch: Any) -> None:
    conn = _FakeConnection(execute_results=["UPDATE 1"])
    service = _make_service(conn)
    responses = [
        "<content>对话内容已模糊化处理</content>",
        "<content>对话内容已模糊化处理</content>",
        "<content>第三次得到有效结果</content>",
    ]

    async def _fake_sleep(_delay: float) -> None:
        return None

    async def _fake_generate_text(**_kwargs: Any) -> str:
        return responses.pop(0)

    monkeypatch.setattr(retry_module.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(
        forgetting_service_module,
        "llm_provider",
        _SimpleNamespace(generate_text=_fake_generate_text),
    )

    ok = asyncio.run(service._fuzzify_conversation(22, "原始总结内容"))

    assert ok is True
    assert responses == []
    assert len(conn.execute_calls) == 1
    update_query, update_args = conn.execute_calls[0]
    assert "UPDATE komari_memory_conversations" in update_query
    assert "DELETE FROM" not in update_query
    assert update_args == ("第三次得到有效结果", 22)


def test_fuzzify_interaction_event_extracts_only_tag_content(monkeypatch: Any) -> None:
    conn = _FakeConnection(execute_results=["UPDATE 1"])
    service = _make_service(conn)

    async def _fake_generate_text(**_kwargs: Any) -> str:
        return "<content>用户倾向于持续进行轻松互动</content>"

    monkeypatch.setattr(
        forgetting_service_module,
        "llm_provider",
        _SimpleNamespace(generate_text=_fake_generate_text),
    )

    ok = asyncio.run(service._fuzzify_interaction_event(30, "原始互动事件"))

    assert ok is True
    update_query, update_args = conn.execute_calls[0]
    assert "UPDATE komari_memory_interaction_history" in update_query
    assert "event_summary = $1" in update_query
    assert "is_fuzzy = TRUE" in update_query
    assert update_args == ("用户倾向于持续进行轻松互动", 30)


def test_fuzzify_interaction_event_deletes_after_placeholder_retries(
    monkeypatch: Any,
) -> None:
    conn = _FakeConnection(execute_results=["DELETE 1"])
    service = _make_service(conn)
    llm_calls = 0

    async def _fake_sleep(_delay: float) -> None:
        return None

    async def _fake_generate_text(**_kwargs: Any) -> str:
        nonlocal llm_calls
        llm_calls += 1
        return "<content>互动事件已模糊化处理</content>"

    monkeypatch.setattr(retry_module.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(
        forgetting_service_module,
        "llm_provider",
        _SimpleNamespace(generate_text=_fake_generate_text),
    )

    ok = asyncio.run(service._fuzzify_interaction_event(31, "原始互动事件"))

    assert ok is True
    assert llm_calls == 3
    delete_query, delete_args = conn.execute_calls[0]
    assert "DELETE FROM komari_memory_interaction_history" in delete_query
    assert "WHERE id = $1" in delete_query
    assert delete_args == (31,)


def test_fuzzify_and_cleanup_interaction_events_continues_after_task_error() -> None:
    rows = [
        {"id": 51, "event_summary": "第一条"},
        {"id": 52, "event_summary": "第二条"},
        {"id": 53, "event_summary": "第三条"},
    ]
    conn = _FakeConnection(fetch_rows=rows)
    service = _make_service(conn)

    async def _fake_fuzzify(event_id: int, original_summary: str) -> bool:
        del original_summary
        if event_id == 52:
            raise RuntimeError("单条互动事件模糊化失败")
        return True

    service._fuzzify_interaction_event = _fake_fuzzify  # type: ignore[method-assign]

    total = asyncio.run(service._fuzzify_and_cleanup_high_value_interaction_events())

    assert total == 2


def test_fuzzify_and_cleanup_interaction_events_skips_bad_records() -> None:
    rows = [
        {"id": 61, "event_summary": "正常事件"},
        {"id": None, "event_summary": "坏 ID"},
        {"id": 63, "event_summary": 123},
    ]
    conn = _FakeConnection(fetch_rows=rows)
    service = _make_service(conn)
    fuzzified_ids: list[int] = []

    async def _fake_fuzzify(event_id: int, original_summary: str) -> bool:
        fuzzified_ids.append(event_id)
        assert original_summary == "正常事件"
        return True

    service._fuzzify_interaction_event = _fake_fuzzify  # type: ignore[method-assign]

    total = asyncio.run(service._fuzzify_and_cleanup_high_value_interaction_events())

    assert total == 1
    assert fuzzified_ids == [61]


def test_fuzzify_and_cleanup_counts_updates_and_retry_deletes(monkeypatch: Any) -> None:
    rows = [
        {"id": 40, "summary": "会成功模糊化"},
        {"id": 41, "summary": "会因占位文本删除"},
    ]
    conn = _FakeConnection(
        execute_results=["DELETE 1", "UPDATE 1", "DELETE 1"],
        fetch_rows=rows,
    )
    service = _make_service(conn)
    cast("Any", service.config).forgetting_fuzzify_concurrency = 1
    llm_calls_by_id: dict[str, int] = {}

    async def _fake_sleep(_delay: float) -> None:
        return None

    async def _fake_generate_text(**kwargs: Any) -> str:
        trace_id = str(kwargs["request_trace_id"])
        llm_calls_by_id[trace_id] = llm_calls_by_id.get(trace_id, 0) + 1
        match trace_id:
            case "memfuzzy-40":
                return "<content>成功模糊后的内容</content>"
            case "memfuzzy-41":
                return "<content>对话内容已模糊化处理</content>"
            case _:
                raise AssertionError

    monkeypatch.setattr(retry_module.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(
        forgetting_service_module,
        "llm_provider",
        _SimpleNamespace(generate_text=_fake_generate_text),
    )

    total = asyncio.run(service._fuzzify_and_cleanup_high_value_memories())

    assert total == 3
    assert llm_calls_by_id == {"memfuzzy-40": 1, "memfuzzy-41": 3}
    assert len(conn.execute_calls) == 3
    update_query, update_args = conn.execute_calls[1]
    delete_query, delete_args = conn.execute_calls[2]
    assert "UPDATE komari_memory_conversations" in update_query
    assert update_args == ("成功模糊后的内容", 40)
    assert "DELETE FROM komari_memory_conversations" in delete_query
    assert delete_args == (41,)
