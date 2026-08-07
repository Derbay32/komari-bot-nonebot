"""Agent Run JSONL、PostgreSQL 轻索引及降级读取测试。"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from komari_bot.plugins.agent_run_logger import reader as reader_module
from komari_bot.plugins.agent_run_logger import storage as storage_module
from komari_bot.plugins.agent_run_logger.reader import AgentRunLogReader
from komari_bot.plugins.agent_run_logger.repository import (
    IndexUnavailableError,
)
from komari_bot.plugins.agent_run_logger.storage import (
    AgentRunLogStorage,
    _append_batch_sync,
    _cleanup_files_sync,
    _remove_legacy_sqlite_sync,
    scan_log_files_sync,
)


def _record(
    run_id: str,
    *,
    minute: int = 0,
    text: str = "完整正文",
) -> dict[str, Any]:
    started = datetime(2026, 7, 22, 10, minute, tzinfo=UTC)
    finished = datetime(2026, 7, 22, 10, minute, 1, tzinfo=UTC)
    return {
        "schema_version": 3,
        "run_id": run_id,
        "trace_id": f"trace-{run_id}",
        "run_type": "chat_reply",
        "task_kind": "chat_reply",
        "origin": "normal",
        "status": "success",
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "duration_ms": 1000.0,
        "input": {"message": text},
        "output": {"reply": f"回复-{run_id}"},
        "error": None,
        "rounds": [
            {
                "method": "generate_messages_completion",
                "model": "deepseek-chat",
                "response": {"reasoning_content": "完整思考"},
            }
        ],
        "tool_executions": [{"tool_name": "search_web", "result": "完整结果"}],
        "errors": [],
        "models": ["deepseek-chat"],
        "methods": ["generate_messages_completion"],
        "usage": {
            "input_tokens": 10,
            "cached_input_tokens": 2,
            "cache_miss_input_tokens": 8,
            "output_tokens": 5,
            "reasoning_output_tokens": 1,
            "total_tokens": 15,
            "call_count": 1,
            "input_tokens_complete": True,
            "cached_input_tokens_complete": True,
            "cache_miss_input_tokens_complete": True,
            "output_tokens_complete": True,
            "reasoning_output_tokens_complete": True,
            "total_tokens_complete": True,
        },
    }


def test_append_uses_one_physical_line_and_private_permissions(tmp_path: Path) -> None:
    entries = _append_batch_sync(
        tmp_path,
        [_record("run-1", text="第一行\n第二行")],
    )

    log_file = tmp_path / "2026-07-22.jsonl"
    physical_lines = log_file.read_bytes().splitlines()
    assert len(physical_lines) == 1
    assert json.loads(physical_lines[0])["input"]["message"] == "第一行\n第二行"
    assert entries[0].byte_offset == 0
    assert entries[0].byte_length == len(physical_lines[0]) + 1
    assert tmp_path.stat().st_mode & 0o777 == 0o700
    assert log_file.stat().st_mode & 0o777 == 0o600


def test_concurrent_writers_keep_every_record_parseable(tmp_path: Path) -> None:
    records = [_record(f"run-{index}", minute=index) for index in range(20)]
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda item: _append_batch_sync(tmp_path, [item]), records))

    lines = (tmp_path / "2026-07-22.jsonl").read_bytes().splitlines()
    parsed = [json.loads(line) for line in lines]
    assert len(parsed) == 20
    assert {item["run_id"] for item in parsed} == {
        f"run-{index}" for index in range(20)
    }


class _FailingThenReconcilingRepository:
    def __init__(self) -> None:
        self.upsert_calls = 0
        self.reconciled_run_ids: list[str] = []
        self.closed = False

    async def initialize(self) -> bool:
        return True

    async def upsert_many(self, _entries: object) -> bool:
        self.upsert_calls += 1
        return False

    async def reconcile(self, entries: list[Any], *, retained_from: date) -> bool:
        del retained_from
        self.reconciled_run_ids = [entry.run_id for entry in entries]
        return True

    async def delete_before(self, _retained_from: date) -> bool:
        return True

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_jsonl_survives_pg_failure_and_reconcile_rebuilds_index(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_repository = _FailingThenReconcilingRepository()
    monkeypatch.setattr(storage_module, "repository", fake_repository)
    service = AgentRunLogStorage(tmp_path)
    service.configure(lambda: 1)

    assert service.enqueue(_record("run-rebuild"))
    await service.flush()
    assert fake_repository.upsert_calls == 1
    assert (tmp_path / "2026-07-22.jsonl").exists()

    await service.reconcile(now=datetime(2026, 7, 22, 12, tzinfo=UTC))
    assert fake_repository.reconciled_run_ids == ["run-rebuild"]
    await service.shutdown()
    assert fake_repository.closed is True


class _UnavailableRepository:
    async def list_entries(self, **_kwargs: object) -> object:
        raise IndexUnavailableError

    async def get(self, _run_id: str) -> object:
        raise IndexUnavailableError


@pytest.mark.asyncio
async def test_reader_falls_back_to_jsonl_and_applies_combined_filters(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _append_batch_sync(tmp_path, [_record("run-fallback")])
    monkeypatch.setattr(reader_module, "repository", _UnavailableRepository())
    log_reader = AgentRunLogReader(
        log_dir=tmp_path,
        now_factory=lambda: datetime(2026, 7, 22, 12, tzinfo=UTC),
    )

    items, total = await log_reader.list_runs(
        log_date="2026-07-22",
        run_type="chat_reply",
        task_kind="chat_reply",
        origin="normal",
        trace_id="trace-run-fallback",
        status="success",
        model="deepseek-chat",
        method="generate_messages_completion",
        limit=1,
    )
    detail = await log_reader.get_run("run-fallback")

    assert total == 1
    assert items[0]["run_id"] == "run-fallback"
    assert items[0]["input_preview"] == '{"message": "完整正文"}'
    assert detail is not None
    assert detail["rounds"][0]["response"]["reasoning_content"] == "完整思考"


def test_cleanup_retains_current_log_day_and_removes_legacy_sqlite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for name in ("2026-07-20.jsonl", "2026-07-21.jsonl", "2026-07-22.jsonl"):
        (tmp_path / name).write_text("{}\n", encoding="utf-8")

    removed = _cleanup_files_sync(tmp_path, date(2026, 7, 22))
    assert removed == 2
    assert [path.name for path in tmp_path.glob("*.jsonl")] == [
        "2026-07-22.jsonl"
    ]

    for suffix in ("", "-wal", "-shm"):
        (tmp_path / f".reply-log-index.sqlite3{suffix}").write_bytes(b"legacy")
    monkeypatch.setattr(storage_module, "LEGACY_LOG_DIR", tmp_path)
    assert _remove_legacy_sqlite_sync() == 3


def test_pg_schema_contains_only_rebuildable_metadata() -> None:
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "migrations/versions/0001_baseline_full_schema.py"
    )
    ddl = migration_path.read_text(encoding="utf-8").lower()
    assert "komari_agent_run_log_index" in ddl
    assert "create unlogged table" in ddl
    assert "byte_offset" in ddl
    assert "byte_length" in ddl
    assert "models" in ddl and "methods" in ddl
    for forbidden in (
        "input_preview",
        "output_preview",
        "prompt_text",
        "reasoning_content",
        "tool_arguments",
        "tool_result",
        "error_text",
    ):
        assert forbidden not in ddl


def test_scan_rebuilds_offsets_for_all_valid_lines(tmp_path: Path) -> None:
    _append_batch_sync(tmp_path, [_record("run-a"), _record("run-b", minute=1)])
    entries = scan_log_files_sync(tmp_path, retained_from=date(2026, 7, 22))
    assert [entry.run_id for entry in entries] == ["run-a", "run-b"]
    assert entries[1].byte_offset == entries[0].byte_length


@pytest.mark.asyncio
async def test_full_queue_drops_whole_record_and_counts_it(tmp_path: Path) -> None:
    service = AgentRunLogStorage(tmp_path)
    service._loop = asyncio.get_running_loop()
    service._queue = asyncio.Queue(maxsize=1)
    queue = service._queue
    queue.put_nowait(None)

    assert service.enqueue(_record("dropped")) is False
    assert service.dropped_count == 1
    assert not list(tmp_path.glob("*.jsonl"))


def test_closing_storage_rejects_new_records_without_starting_writer(
    tmp_path: Path,
) -> None:
    service = AgentRunLogStorage(tmp_path)
    service._closing = True

    assert service.enqueue(_record("too-late")) is False
    assert service.dropped_count == 1
    assert service._worker is None
