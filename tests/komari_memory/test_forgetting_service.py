"""ForgettingService tests."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import date
from types import SimpleNamespace
from types import SimpleNamespace as _SimpleNamespace
from typing import Any, cast

import pytest

from komari_bot.plugins.komari_memory.core import retry as retry_module
from komari_bot.plugins.komari_memory.services import (
    forgetting_service as forgetting_service_module,
)
from komari_bot.plugins.komari_memory.services.forgetting_service import (
    ForgettingService,
    FuzzifyBatchError,
)


class _FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb


class _FakeConnection:
    def __init__(
        self,
        *,
        execute_results: list[str] | None = None,
        fetch_rows: list[dict[str, Any]] | None = None,
        fetchval_results: list[object] | None = None,
    ) -> None:
        self.execute_results = list(execute_results or [])
        self.fetch_rows = list(fetch_rows or [])
        self.fetchval_results = list(fetchval_results or [])
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchval_calls: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction()

    async def execute(self, query: str, *args: object) -> str:
        self.execute_calls.append((query, args))
        if self.execute_results:
            return self.execute_results.pop(0)
        return "DELETE 0"

    async def fetch(self, query: str, *args: object) -> list[dict[str, Any]]:
        self.fetch_calls.append((query, args))
        return list(self.fetch_rows)

    async def fetchval(self, query: str, *args: object) -> object | None:
        self.fetchval_calls.append((query, args))
        if self.fetchval_results:
            return self.fetchval_results.pop(0)
        if "UPDATE komari_memory_conversations" in query:
            return args[1]
        if "UPDATE komari_memory_interaction_history" in query:
            return args[1]
        if "DELETE FROM komari_memory_" in query:
            return args[0]
        return None


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


class _FakeForgettingJobRepository:
    def __init__(self) -> None:
        self.stage: str | None = None
        self.owner_token: str | None = None
        self.actions: list[str] = []
        self.advance_calls: list[tuple[str, str]] = []
        self.failure_codes: list[str] = []
        self.fail_once_at_stage: str | None = None

    async def claim(self, **kwargs: Any) -> SimpleNamespace:
        owner_token = str(kwargs["owner_token"])
        if self.stage == "completed":
            return SimpleNamespace(status="completed", stage="completed")
        if self.owner_token is not None and self.owner_token != owner_token:
            return SimpleNamespace(status="busy", stage=self.stage or "claimed")
        self.owner_token = owner_token
        self.stage = self.stage or "claimed"
        return SimpleNamespace(status="claimed", stage=self.stage)

    async def run_transactional_stage(self, **kwargs: Any) -> tuple[str, ...]:
        expected_stage = str(kwargs["expected_stage"])
        assert kwargs["owner_token"] == self.owner_token
        assert expected_stage == self.stage
        if self.fail_once_at_stage == expected_stage:
            self.fail_once_at_stage = None
            raise RuntimeError("模拟阶段事务失败")
        actions = kwargs["actions"]
        self.actions.extend(str(query) for query, _params in actions)
        self.stage = str(kwargs["next_stage"])
        return tuple("OK" for _action in actions)

    async def advance_stage(self, **kwargs: Any) -> None:
        expected_stage = str(kwargs["expected_stage"])
        next_stage = str(kwargs["next_stage"])
        assert kwargs["owner_token"] == self.owner_token
        assert expected_stage == self.stage
        self.advance_calls.append((expected_stage, next_stage))
        self.stage = next_stage

    async def renew(self, **kwargs: Any) -> bool:
        return kwargs["owner_token"] == self.owner_token and self.stage != "completed"

    async def mark_failure(self, **kwargs: Any) -> None:
        if kwargs["owner_token"] == self.owner_token:
            self.failure_codes.append(str(kwargs["error_code"]))
            self.owner_token = None


def _make_service(
    conn: _FakeConnection,
    *,
    job_repository: Any | None = None,
    embedding_plugin: Any = forgetting_service_module.embedding_provider,
) -> ForgettingService:
    config = SimpleNamespace(
        forgetting_enabled=True,
        forgetting_importance_threshold=3,
        forgetting_min_age_days=7,
        forgetting_decay_factor=0.95,
        forgetting_fuzzify_concurrency=2,
        forgetting_job_lease_seconds=900,
        response_tag="content",
        llm_model_summary="summary-model",
        llm_temperature_summary=0.3,
        llm_max_tokens_summary=256,
        llm_thinking_mode_chat=False,
        llm_reasoning_effort_chat="",
        llm_thinking_mode_summary=False,
        llm_reasoning_effort_summary="",
    )
    return ForgettingService(
        pg_pool=cast("Any", _FakePool(conn)),
        config_provider=lambda: cast("Any", config),
        job_repository=job_repository,
        embedding_plugin=embedding_plugin,
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


def test_forgetting_service_reads_current_config_for_each_execution() -> None:
    conn = _FakeConnection(execute_results=["DELETE 1", "DELETE 1"])
    current = {
        "config": SimpleNamespace(
            forgetting_importance_threshold=2,
            forgetting_min_age_days=5,
        )
    }
    service = ForgettingService(
        pg_pool=cast("Any", _FakePool(conn)),
        config_provider=lambda: cast("Any", current["config"]),
    )

    asyncio.run(service._delete_low_value_memories())
    current["config"] = SimpleNamespace(
        forgetting_importance_threshold=4,
        forgetting_min_age_days=9,
    )
    asyncio.run(service._delete_low_value_memories())

    assert [args for _query, args in conn.execute_calls] == [(2, 5), (4, 9)]


def test_daily_decay_uses_integer_step_down() -> None:
    conn = _FakeConnection(execute_results=["UPDATE 4"])
    service = _make_service(conn)

    asyncio.run(service._daily_decay())

    assert len(conn.execute_calls) == 1
    query, args = conn.execute_calls[0]
    assert "GREATEST(importance_current - 1, 0)" in query
    assert args == ()


def test_daily_forgetting_job_runs_each_stage_only_once_per_date() -> None:
    conn = _FakeConnection()
    jobs = _FakeForgettingJobRepository()
    first = _make_service(conn, job_repository=jobs)
    second = _make_service(conn, job_repository=jobs)

    async def _no_fuzzify() -> int:
        return 0

    first._fuzzify_and_cleanup_high_value_memories = _no_fuzzify  # type: ignore[method-assign]
    first._fuzzify_and_cleanup_high_value_interaction_events = _no_fuzzify  # type: ignore[method-assign]
    second._fuzzify_and_cleanup_high_value_memories = _no_fuzzify  # type: ignore[method-assign]
    second._fuzzify_and_cleanup_high_value_interaction_events = _no_fuzzify  # type: ignore[method-assign]
    run_date = date(2026, 7, 17)

    first_result = asyncio.run(first.decay_and_cleanup(run_date=run_date))
    second_result = asyncio.run(second.decay_and_cleanup(run_date=run_date))

    assert first_result is True
    assert second_result is False
    assert jobs.stage == "completed"
    assert sum("komari_memory_conversations" in query for query in jobs.actions) == 2
    assert sum("komari_memory_interaction_history" in query for query in jobs.actions) == 2
    assert jobs.advance_calls == [
        ("low_value_cleanup_done", "conversation_fuzzify_done"),
        ("conversation_fuzzify_done", "completed"),
    ]


def test_daily_forgetting_job_resumes_without_repeating_completed_decay_stage() -> None:
    conn = _FakeConnection()
    jobs = _FakeForgettingJobRepository()
    jobs.fail_once_at_stage = "conversation_decay_done"
    first = _make_service(conn, job_repository=jobs)
    second = _make_service(conn, job_repository=jobs)

    async def _no_fuzzify() -> int:
        return 0

    first._fuzzify_and_cleanup_high_value_memories = _no_fuzzify  # type: ignore[method-assign]
    first._fuzzify_and_cleanup_high_value_interaction_events = _no_fuzzify  # type: ignore[method-assign]
    second._fuzzify_and_cleanup_high_value_memories = _no_fuzzify  # type: ignore[method-assign]
    second._fuzzify_and_cleanup_high_value_interaction_events = _no_fuzzify  # type: ignore[method-assign]
    run_date = date(2026, 7, 17)

    with pytest.raises(RuntimeError, match="阶段事务失败"):
        asyncio.run(first.decay_and_cleanup(run_date=run_date))
    resumed = asyncio.run(second.decay_and_cleanup(run_date=run_date))

    assert resumed is True
    assert jobs.failure_codes == ["RuntimeError"]
    assert jobs.stage == "completed"
    conversation_decay_actions = [
        query
        for query in jobs.actions
        if "UPDATE komari_memory_conversations" in query
    ]
    interaction_decay_actions = [
        query
        for query in jobs.actions
        if "UPDATE komari_memory_interaction_history" in query
    ]
    assert len(conversation_decay_actions) == 1
    assert len(interaction_decay_actions) == 1


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


def test_fuzzify_batch_error_keeps_job_stage_retryable() -> None:
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

    with pytest.raises(FuzzifyBatchError, match="1 条失败记录"):
        asyncio.run(service._fuzzify_and_cleanup_high_value_memories())


def test_fuzzify_bad_records_prevent_stage_completion() -> None:
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

    with pytest.raises(FuzzifyBatchError, match="2 条失败记录"):
        asyncio.run(service._fuzzify_and_cleanup_high_value_memories())

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
    assert 'source_type="memory"' in llm_calls[0]["prompt"]
    update_query, update_args = conn.fetchval_calls[0]
    assert "is_fuzzy = TRUE" in update_query
    assert "importance_current = importance_initial" in update_query
    assert "summary = $3" in update_query
    assert update_args == ("模糊后的结果", 10, "原始总结内容")
    embedding_query, embedding_args = conn.execute_calls[0]
    assert "komari_memory_conversation_embeddings" in embedding_query
    assert embedding_args == (
        10,
        hashlib.sha256("模糊后的结果".encode()).hexdigest(),
        "[0.1, 0.2, 0.3]",
    )
    assert embedding_args[1] != hashlib.sha256("原始总结内容".encode()).hexdigest()


def test_forgetting_context_is_untrusted_and_explicitly_bounded() -> None:
    original = "&" * 2_000

    rendered = forgetting_service_module._render_bounded_memory_context(
        content=original,
        source_id="forgetting-conversation:1",
    )

    assert 'source_type="memory"' in rendered
    assert '"original_characters":2000' in rendered
    assert '"truncated":true' in rendered
    assert original not in rendered
    assert len(rendered) <= (
        forgetting_service_module._FORGETTING_RENDERED_CONTEXT_BUDGET.max_characters
    )


def test_fuzzify_embedding_failure_deletes_old_vector_in_same_transaction(
    monkeypatch: Any,
) -> None:
    class _FailingEmbedding:
        async def embed(self, _text: str) -> list[float]:
            raise RuntimeError("向量服务不可用")

    conn = _FakeConnection()
    service = _make_service(conn, embedding_plugin=_FailingEmbedding())

    async def _fake_generate_text(**_kwargs: Any) -> str:
        return "<content>只保留模糊主题</content>"

    monkeypatch.setattr(
        forgetting_service_module,
        "llm_provider",
        _SimpleNamespace(generate_text=_fake_generate_text),
    )

    ok = asyncio.run(service._fuzzify_conversation(12, "包含可识别旧细节"))

    assert ok is True
    update_query, update_args = conn.fetchval_calls[0]
    delete_vector_query, delete_vector_args = conn.execute_calls[0]
    assert "summary = $3" in update_query
    assert update_args == ("只保留模糊主题", 12, "包含可识别旧细节")
    assert "DELETE FROM komari_memory_conversation_embeddings" in delete_vector_query
    assert delete_vector_args == (12,)


def test_fuzzify_cas_does_not_overwrite_concurrently_touched_memory(
    monkeypatch: Any,
) -> None:
    conn = _FakeConnection(fetchval_results=[None])
    service = _make_service(conn)

    async def _fake_generate_text(**_kwargs: Any) -> str:
        return "<content>过期任务生成的模糊主题</content>"

    monkeypatch.setattr(
        forgetting_service_module,
        "llm_provider",
        _SimpleNamespace(generate_text=_fake_generate_text),
    )

    ok = asyncio.run(service._fuzzify_conversation(13, "被并发访问的原正文"))

    assert ok is False
    update_query, update_args = conn.fetchval_calls[0]
    assert "importance_current = 0" in update_query
    assert "is_fuzzy = FALSE" in update_query
    assert update_args == ("过期任务生成的模糊主题", 13, "被并发访问的原正文")
    assert conn.execute_calls == []


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
    assert conn.execute_calls == []
    delete_query, delete_args = conn.fetchval_calls[0]
    assert "DELETE FROM komari_memory_conversations" in delete_query
    assert "WHERE id = $1" in delete_query
    assert "summary = $2" in delete_query
    assert delete_args == (20, "原始总结内容")


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
    delete_query, delete_args = conn.fetchval_calls[0]
    assert "DELETE FROM komari_memory_conversations" in delete_query
    assert delete_args == (21, "原始总结内容")


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
    update_query, update_args = conn.fetchval_calls[0]
    assert "UPDATE komari_memory_conversations" in update_query
    assert "DELETE FROM" not in update_query
    assert update_args == ("第三次得到有效结果", 22, "原始总结内容")


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
    update_query, update_args = conn.fetchval_calls[0]
    assert "UPDATE komari_memory_interaction_history" in update_query
    assert "event_summary = $1" in update_query
    assert "is_fuzzy = TRUE" in update_query
    assert update_args == (
        "用户倾向于持续进行轻松互动",
        30,
        "原始互动事件",
    )
    embedding_query, embedding_args = conn.execute_calls[0]
    assert "komari_memory_interaction_embeddings" in embedding_query
    assert embedding_args[0] == 30
    assert embedding_args[1] == hashlib.sha256(
        "用户倾向于持续进行轻松互动".encode()
    ).hexdigest()


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
    delete_query, delete_args = conn.fetchval_calls[0]
    assert "DELETE FROM komari_memory_interaction_history" in delete_query
    assert "WHERE id = $1" in delete_query
    assert delete_args == (31, "原始互动事件")


def test_interaction_fuzzify_error_keeps_job_stage_retryable() -> None:
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

    with pytest.raises(FuzzifyBatchError, match="1 条失败记录"):
        asyncio.run(service._fuzzify_and_cleanup_high_value_interaction_events())


def test_invalid_interaction_records_prevent_stage_completion() -> None:
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

    with pytest.raises(FuzzifyBatchError, match="2 条失败记录"):
        asyncio.run(service._fuzzify_and_cleanup_high_value_interaction_events())

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
    assert len(conn.execute_calls) == 2
    update_query, update_args = conn.fetchval_calls[0]
    delete_query, delete_args = conn.fetchval_calls[1]
    assert "UPDATE komari_memory_conversations" in update_query
    assert update_args == ("成功模糊后的内容", 40, "会成功模糊化")
    assert "DELETE FROM komari_memory_conversations" in delete_query
    assert delete_args == (41, "会因占位文本删除")
