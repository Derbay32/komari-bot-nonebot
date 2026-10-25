"""Acceptance tests for the standalone database-memory Hindsight migration.

The tests drive the real CLI orchestration through the two external boundaries
only: PostgreSQL (asyncpg) for export and the official Hindsight client for
import.  No bot runtime module is imported by the script or by these tests.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import stat
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "migrate_memory_to_hindsight.py"
CONVERSATIONS = "komari_memory_conversations"
PROFILES = "komari_memory_user_profile"
INTERACTIONS = "komari_memory_interaction_history"


def _load_script_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "tests.scripts.migrate_memory_to_hindsight",
        SCRIPT_PATH,
    )
    if spec is None or spec.loader is None:
        raise AssertionError("script module could not be loaded")  # noqa: TRY003

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def _unexpected_connect(_dsn: str, _timeout: float) -> Any:
    raise AssertionError("export must not create a Hindsight client")  # noqa: TRY003


def _unexpected_client(_target: Any, _timeout: float) -> Any:
    raise AssertionError("this phase must not create a Hindsight client")  # noqa: TRY003


class _FakeClock:
    def __init__(self, *, start: float = 0.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _runtime(
    module: Any,
    *,
    connect: Callable[[str, float], Any] | None = None,
    client: Callable[[Any, float], Any] | None = None,
    clock: _FakeClock | None = None,
) -> Any:
    return module.MigrationBoundaries(
        connect_source=connect or _unexpected_connect,
        create_client=client or _unexpected_client,
        monotonic=clock.monotonic if clock is not None else time.monotonic,
        sleep=clock.sleep if clock is not None else asyncio.sleep,
    )


class _FakeTransaction:
    def __init__(
        self, connection: "_FakeSourceConnection", kwargs: dict[str, Any]
    ) -> None:
        self.connection = connection
        self.kwargs = kwargs

    async def __aenter__(self) -> None:
        self.connection.transaction_kwargs.append(self.kwargs)
        self.connection.transaction_active = True

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb
        self.connection.transaction_active = False


class _FakeSourceConnection:
    def __init__(
        self,
        rows_by_table: dict[str, list[dict[str, Any]]],
        *,
        entity_rows: int | None = None,
        legacy_rows: dict[str, int] | None = None,
        fail_table: str | None = None,
    ) -> None:
        self.rows_by_table = rows_by_table
        self.entity_rows = entity_rows
        self.legacy_rows = legacy_rows or {}
        self.fail_table = fail_table
        self.fetch_queries: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchval_queries: list[tuple[str, tuple[Any, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.transaction_kwargs: list[dict[str, Any]] = []
        self.transaction_active = False
        self.closed = False

    def transaction(self, **kwargs: Any) -> _FakeTransaction:
        return _FakeTransaction(self, kwargs)

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_queries.append((query, args))
        if "FROM pg_class" in query:
            return [{"relname": name} for name in sorted(self.legacy_rows)]
        for table, rows in self.rows_by_table.items():
            if f"FROM {table}" in query:
                if self.fail_table == table:
                    raise RuntimeError("source read failed")  # noqa: TRY003
                return rows
        raise AssertionError(f"unexpected export query: {query}")  # noqa: TRY003

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.fetchval_queries.append((query, args))
        if "to_regclass" in query:
            table = str(args[0])
            return table in self.rows_by_table or (
                table == "komari_memory_entity" and self.entity_rows is not None
            )
        if "COUNT(*)" in query:
            for table, count in self.legacy_rows.items():
                if f'FROM "{table}"' in query:
                    return count
            if 'FROM "komari_memory_entity"' in query:
                return self.entity_rows or 0
        raise AssertionError(f"unexpected export scalar query: {query}")  # noqa: TRY003

    async def execute(self, query: str, *args: Any) -> str:
        self.execute_calls.append((query, args))
        return "OK"

    async def close(self) -> None:
        self.closed = True

    @property
    def all_queries(self) -> list[str]:
        return [
            query
            for query, _args in (
                *self.fetch_queries,
                *self.fetchval_queries,
                *self.execute_calls,
            )
        ]


def _conversation_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": 7,
        "group_id": "group-100",
        "summary": "当前摘要：一起去看了海 ✓",
        "participants": ["user-a", "user-b"],
        "dedup_key": None,
        "start_time": datetime(2026, 1, 2, 3, 4, 5, 123456),  # noqa: DTZ001
        "end_time": datetime(2026, 1, 2, 4, 5, 6, 654321),  # noqa: DTZ001
        "importance": 3,
        "importance_initial": 2,
        "importance_current": 1,
        "last_accessed": datetime(2026, 1, 3, 5, 6, 7, tzinfo=UTC),
        "is_fuzzy": True,
        "created_at": datetime(2026, 1, 2, 4, 5, 6, 111111, tzinfo=UTC),
    }
    row.update(overrides)
    return row


def _profile_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "user_id": "user-a",
        "group_id": "group-100",
        "version": 4,
        "display_name": "小鞠",
        "traits": {
            "风格": {"tone": "温柔", "tags": ["安静", None]},
            "count": 2,
        },
        "updated_at": datetime(2026, 2, 3, 4, 5, 6, 222222, tzinfo=UTC),
        "importance": 5,
        "access_count": 9,
        "last_accessed": datetime(2026, 2, 4, 5, 6, 7, tzinfo=UTC),
    }
    row.update(overrides)
    return row


def _interaction_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": 11,
        "user_id": "user-b",
        "display_name": "八奈见",
        "event_summary": "模糊事件：在食堂交换了便当",
        "source_dedup_key": None,
        "source_message_count": 0,
        "first_seen_at": datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC),
        "last_seen_at": datetime(2026, 3, 4, 6, 7, 8, tzinfo=UTC),
        "importance": 4,
        "importance_initial": 3,
        "importance_current": 5,
        "last_accessed": datetime(2026, 3, 5, 7, 8, 9, tzinfo=UTC),
        "is_fuzzy": False,
        "created_at": None,
    }
    row.update(overrides)
    return row


def _default_rows() -> dict[str, list[dict[str, Any]]]:
    return {
        CONVERSATIONS: [_conversation_row()],
        PROFILES: [_profile_row()],
        INTERACTIONS: [_interaction_row()],
    }


def _export(
    module: Any,
    tmp_path: Path,
    monkeypatch: Any,
    *,
    source_id: str = "prod-deploy-1",
    connection: _FakeSourceConnection | None = None,
    output_name: str = "manifest.json",
) -> tuple[int, Path, _FakeSourceConnection, list[tuple[str, float]]]:
    manifest_path = tmp_path / output_name
    connection = connection or _FakeSourceConnection(_default_rows())
    connect_calls: list[tuple[str, float]] = []

    async def _connect(dsn: str, request_timeout: float) -> Any:
        connect_calls.append((dsn, request_timeout))
        return connection

    monkeypatch.setenv(
        "SQLALCHEMY_DATABASE_URL",
        "postgresql+asyncpg://user:pass@localhost:5432/komari_bot",
    )
    runtime = _runtime(module, connect=_connect)
    exit_code = asyncio.run(
        module.run_cli(
            [
                "export",
                "--source-id",
                source_id,
                "--output",
                str(manifest_path),
            ],
            boundaries=runtime,
        )
    )
    return exit_code, manifest_path, connection, connect_calls


def test_export_preserves_every_column_null_json_and_timestamp(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_script_module()

    exit_code, manifest_path, connection, connect_calls = _export(
        module, tmp_path, monkeypatch
    )

    assert exit_code == 0
    assert connection.closed is True
    assert connect_calls == [("postgresql://user:pass@localhost:5432/komari_bot", 60.0)]
    assert connection.execute_calls == []
    assert connection.transaction_kwargs == [
        {"isolation": "repeatable_read", "readonly": True}
    ]
    assert all(
        query.strip().upper().startswith("SELECT") for query in connection.all_queries
    )
    assert not any("embedding" in query for query in connection.all_queries)
    assert not any("komari_memory_jobs" in query for query in connection.all_queries)

    manifest_text = manifest_path.read_text(encoding="utf-8")
    assert "postgresql://" not in manifest_text
    assert "user:pass" not in manifest_text
    manifest = json.loads(manifest_text)
    assert manifest["kind"] == "komari-memory-export"
    assert manifest["schema_version"] == 1
    assert manifest["source_id"] == "prod-deploy-1"
    assert manifest["counts"] == {
        CONVERSATIONS: 1,
        PROFILES: 1,
        INTERACTIONS: 1,
    }
    assert len(manifest["records"]) == 3
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600

    by_table = {record["table"]: record for record in manifest["records"]}
    conversation = by_table[CONVERSATIONS]
    assert conversation["primary_key"] == {"id": 7}
    assert conversation["provenance"] == {"group_id": "group-100"}
    assert conversation["fields"] == {
        "id": 7,
        "group_id": "group-100",
        "summary": "当前摘要：一起去看了海 ✓",
        "participants": ["user-a", "user-b"],
        "dedup_key": None,
        "start_time": "2026-01-02T03:04:05.123456",
        "end_time": "2026-01-02T04:05:06.654321",
        "importance": 3,
        "importance_initial": 2,
        "importance_current": 1,
        "last_accessed": "2026-01-03T05:06:07+00:00",
        "is_fuzzy": True,
        "created_at": "2026-01-02T04:05:06.111111+00:00",
    }

    profile = by_table[PROFILES]
    assert profile["primary_key"] == {
        "user_id": "user-a",
        "group_id": "group-100",
    }
    assert profile["fields"]["traits"] == {
        "风格": {"tone": "温柔", "tags": ["安静", None]},
        "count": 2,
    }
    assert profile["fields"]["updated_at"] == "2026-02-03T04:05:06.222222+00:00"

    interaction = by_table[INTERACTIONS]
    assert interaction["primary_key"] == {"id": 11}
    assert interaction["provenance"] == {}
    assert "group_id" not in interaction["provenance"]
    assert interaction["fields"]["created_at"] is None
    assert interaction["fields"]["source_dedup_key"] is None
    assert interaction["fields"]["importance_current"] == 5
    assert interaction["fields"]["is_fuzzy"] is False

    for record in manifest["records"]:
        assert record["source_text"].startswith(f"record_kind: {record['kind']}")
        assert "identity: " in record["source_text"]
        assert "provenance: " in record["source_text"]
        assert "content: " in record["source_text"]
        assert record["fields"]
    assert "当前摘要：一起去看了海 ✓" in by_table[CONVERSATIONS]["source_text"]
    assert "模糊事件：在食堂交换了便当" in by_table[INTERACTIONS]["source_text"]
    assert "风格" in by_table[PROFILES]["source_text"]


def test_export_document_ids_are_stable_and_ignore_mutable_content(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_script_module()
    first_rows = _default_rows()
    second_rows = _default_rows()
    second_rows[CONVERSATIONS][0]["summary"] = "改写后的摘要"
    second_rows[INTERACTIONS][0]["event_summary"] = "改写后的事件"

    _code, first_path, _conn, _calls = _export(
        module, tmp_path, monkeypatch, output_name="first.json"
    )
    _code, second_path, _conn, _calls = _export(
        module,
        tmp_path,
        monkeypatch,
        connection=_FakeSourceConnection(second_rows),
        output_name="second.json",
    )
    _code, other_source_path, _conn, _calls = _export(
        module,
        tmp_path,
        monkeypatch,
        source_id="prod-deploy-2",
        output_name="other-source.json",
    )
    code, _conn, _calls, _connect_calls = _export(
        module,
        tmp_path,
        monkeypatch,
        connection=_FakeSourceConnection(first_rows),
        output_name="third.json",
    )

    assert code == 0
    first = json.loads(first_path.read_text(encoding="utf-8"))
    second = json.loads(second_path.read_text(encoding="utf-8"))
    other_source = json.loads(other_source_path.read_text(encoding="utf-8"))

    first_ids = {record["table"]: record["document_id"] for record in first["records"]}
    second_ids = {
        record["table"]: record["document_id"] for record in second["records"]
    }
    other_ids = {
        record["table"]: record["document_id"] for record in other_source["records"]
    }
    assert first_ids == second_ids
    assert all(
        first_ids[table] != other_ids[table]
        for table in (CONVERSATIONS, PROFILES, INTERACTIONS)
    )


def test_export_rejects_non_empty_legacy_sources_without_writing_manifest(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    module = _load_script_module()
    legacy = "komari_memory_interaction_history_legacy_2025"
    connection = _FakeSourceConnection(
        _default_rows(),
        entity_rows=2,
        legacy_rows={legacy: 1},
    )
    output_path = tmp_path / "manifest.json"

    async def _connect(_dsn: str, _request_timeout: float) -> Any:
        return connection

    monkeypatch.setenv(
        "SQLALCHEMY_DATABASE_URL",
        "postgresql+asyncpg://user:pass@localhost:5432/komari_bot",
    )
    exit_code = asyncio.run(
        module.run_cli(
            [
                "export",
                "--source-id",
                "prod-deploy-1",
                "--output",
                str(output_path),
            ],
            boundaries=_runtime(module, connect=_connect),
        )
    )

    assert exit_code == 1
    assert not output_path.exists()
    captured = capsys.readouterr()
    assert module.ERROR_EXPORT_UNSUPPORTED_SOURCE in captured.err
    assert "komari_memory_entity" in captured.err
    assert legacy in captured.err
    assert "当前摘要" not in captured.err
    assert "模糊事件" not in captured.err


def test_export_allows_empty_legacy_tables_and_records_their_inventory(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_script_module()
    legacy = "komari_memory_interaction_history_legacy_2025"
    connection = _FakeSourceConnection(
        _default_rows(),
        entity_rows=0,
        legacy_rows={legacy: 0},
    )

    exit_code, manifest_path, _connection, _calls = _export(
        module,
        tmp_path,
        monkeypatch,
        connection=connection,
    )

    assert exit_code == 0
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["excluded_sources"] == [
        {"table": "komari_memory_entity", "row_count": 0},
        {"table": legacy, "row_count": 0},
    ]


def test_export_failure_never_replaces_an_existing_manifest(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_script_module()
    output_path = tmp_path / "manifest.json"
    output_path.write_text("previous manifest", encoding="utf-8")
    connection = _FakeSourceConnection(_default_rows(), fail_table=INTERACTIONS)

    async def _connect(_dsn: str, _request_timeout: float) -> Any:
        return connection

    monkeypatch.setenv(
        "SQLALCHEMY_DATABASE_URL",
        "postgresql+asyncpg://user:pass@localhost:5432/komari_bot",
    )
    exit_code = asyncio.run(
        module.run_cli(
            [
                "export",
                "--source-id",
                "prod-deploy-1",
                "--output",
                str(output_path),
            ],
            boundaries=_runtime(module, connect=_connect),
        )
    )

    assert exit_code == 1
    assert output_path.read_text(encoding="utf-8") == "previous manifest"
    assert list(tmp_path.iterdir()) == [output_path]
    assert connection.execute_calls == []


_UNSET = object()


class _FakeApiError(Exception):
    def __init__(self, status: int | None = None) -> None:
        super().__init__("fake api failure")
        self.status = status


class _FakeBanks:
    def __init__(self, client: Any) -> None:
        self.client = client

    async def get_bank_profile(self, bank_id: str, *, _request_timeout: float) -> Any:
        self.client.bank_profile_calls.append((bank_id, _request_timeout))
        if self.client.bank_error is not None:
            raise self.client.bank_error
        if (
            self.client.bank_profile_delay
            and len(self.client.bank_profile_calls)
            >= self.client.bank_profile_delay_from_call
        ):
            await asyncio.sleep(self.client.bank_profile_delay)
            self.client.bank_profile_delay_finished = True
        if self.client.bank_profile_response is not _UNSET:
            return self.client.bank_profile_response
        return {"bank_id": bank_id}


class _FakeDocuments:
    def __init__(self, client: Any) -> None:
        self.client = client

    async def get_document(
        self, bank_id: str, document_id: str, *, _request_timeout: float
    ) -> Any:
        self.client.document_calls.append((bank_id, document_id, _request_timeout))
        if self.client.document_error is not None:
            raise self.client.document_error
        document = self.client.document_store.get(document_id)
        if document is None:
            raise _FakeApiError(404)
        return document


class _FakeOperations:
    def __init__(self, client: Any) -> None:
        self.client = client

    async def get_operation_status(
        self, bank_id: str, operation_id: str, *, _request_timeout: float
    ) -> Any:
        self.client.operation_calls.append((bank_id, operation_id, _request_timeout))
        if self.client.operation_error is not None:
            raise self.client.operation_error
        document_id = self.client.operation_documents.get(operation_id)
        statuses = self.client.operation_statuses.get(document_id)
        if statuses is None:
            statuses = self.client.default_operation_statuses
        status = statuses.pop(0) if len(statuses) > 1 else statuses[0]
        if self.client.on_operation_status is not None:
            self.client.on_operation_status(status)
        if self.client.operation_status_delay:
            await asyncio.sleep(self.client.operation_status_delay)
            self.client.operation_status_delay_finished = True
        response = {"status": status, "operation_id": operation_id}
        if self.client.operation_response_operation_id is not _UNSET:
            if self.client.operation_response_operation_id is None:
                response.pop("operation_id")
            else:
                response["operation_id"] = self.client.operation_response_operation_id
        return response


class _FakeHindsightClient:
    def __init__(
        self,
        *,
        api_version: str = "1.1.0",
        bank_error: Exception | None = None,
        bank_profile_response: Any = _UNSET,
        bank_profile_delay: float = 0.0,
        bank_profile_delay_from_call: int = 1,
        default_operation_statuses: list[str] | None = None,
        operation_statuses: dict[str, list[str]] | None = None,
        operation_status_delay: float = 0.0,
        operation_response_operation_id: Any = _UNSET,
        on_operation_status: Callable[[str], None] | None = None,
        operation_error: Exception | None = None,
        document_error: Exception | None = None,
        documents: dict[str, Any] | None = None,
        retain_error: Exception | None = None,
        retain_error_by_document: dict[str, Exception] | None = None,
        retain_creates_document: bool = True,
        retain_document_text: Any = _UNSET,
        retain_document_metadata: Any = _UNSET,
        receipt_overrides: dict[str, Any] | None = None,
        on_retain: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.api_version = api_version
        self.bank_error = bank_error
        self.bank_profile_response = bank_profile_response
        self.bank_profile_delay = bank_profile_delay
        self.bank_profile_delay_from_call = bank_profile_delay_from_call
        self.bank_profile_delay_finished = False
        self.default_operation_statuses = default_operation_statuses or ["completed"]
        self.operation_statuses = operation_statuses or {}
        self.operation_status_delay = operation_status_delay
        self.operation_status_delay_finished = False
        self.operation_response_operation_id = operation_response_operation_id
        self.on_operation_status = on_operation_status
        self.operation_error = operation_error
        self.document_error = document_error
        self.document_store = documents or {}
        self.retain_error = retain_error
        self.retain_error_by_document = retain_error_by_document or {}
        self.retain_creates_document = retain_creates_document
        self.retain_document_text = retain_document_text
        self.retain_document_metadata = retain_document_metadata
        self.receipt_overrides = receipt_overrides or {}
        self.on_retain = on_retain
        self.retain_calls: list[dict[str, Any]] = []
        self.bank_profile_calls: list[tuple[str, float]] = []
        self.document_calls: list[tuple[str, str, float]] = []
        self.operation_calls: list[tuple[str, str, float]] = []
        self.operation_documents: dict[str, str] = {}
        self.mutation_calls: list[str] = []
        self.version_calls = 0
        self.closed = False
        self.banks = _FakeBanks(self)
        self.documents = _FakeDocuments(self)
        self.operations = _FakeOperations(self)

    async def aget_version(self) -> Any:
        self.version_calls += 1
        return {"api_version": self.api_version, "features": {}}

    async def aretain_batch(self, **kwargs: Any) -> Any:
        self.retain_calls.append(kwargs)
        if self.on_retain is not None:
            self.on_retain(kwargs)
        document_id = str(kwargs["document_id"])
        error = self.retain_error_by_document.get(document_id, self.retain_error)
        if error is not None:
            raise error
        operation_id = str(kwargs["operation_id"])
        self.operation_documents[operation_id] = document_id
        item = kwargs["items"][0]
        if self.retain_creates_document:
            retained_text = (
                item["content"]
                if self.retain_document_text is _UNSET
                else self.retain_document_text
            )
            retained_metadata = (
                item.get("metadata")
                if self.retain_document_metadata is _UNSET
                else self.retain_document_metadata
            )
            self.document_store[document_id] = {
                "id": document_id,
                "bank_id": kwargs["bank_id"],
                "original_text": retained_text,
                "content_hash": None,
                "document_metadata": retained_metadata,
            }
        response = {
            "success": True,
            "bank_id": kwargs["bank_id"],
            "items_count": 1,
            "var_async": True,
            "operation_id": operation_id,
        }
        response.update(self.receipt_overrides)
        return response

    async def aclose(self) -> None:
        self.closed = True

    async def create_bank(self, *_args: Any, **_kwargs: Any) -> Any:
        self.mutation_calls.append("create_bank")
        raise AssertionError("create_bank must never be called")  # noqa: TRY003

    async def update_bank_config(self, *_args: Any, **_kwargs: Any) -> Any:
        self.mutation_calls.append("update_bank_config")
        raise AssertionError("update_bank_config must never be called")  # noqa: TRY003

    async def update_bank(self, *_args: Any, **_kwargs: Any) -> Any:
        self.mutation_calls.append("update_bank")
        raise AssertionError("update_bank must never be called")  # noqa: TRY003

    async def delete_document(self, *_args: Any, **_kwargs: Any) -> Any:
        self.mutation_calls.append("delete_document")
        raise AssertionError("delete_document must never be called")  # noqa: TRY003

    async def update_document(self, *_args: Any, **_kwargs: Any) -> Any:
        self.mutation_calls.append("update_document")
        raise AssertionError("update_document must never be called")  # noqa: TRY003


def _document(
    *,
    document_id: str,
    bank_id: str,
    text: str | None,
    metadata: dict[str, Any] | None,
    retain_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": document_id,
        "bank_id": bank_id,
        "original_text": text,
        "content_hash": None,
        "document_metadata": metadata,
        "retain_params": retain_params,
    }


def _expected_metadata(
    record: dict[str, Any],
    *,
    source_id: str,
    manifest_sha256: str,
) -> dict[str, str]:
    return {
        "komari_migration_source_id": source_id,
        "komari_migration_table": record["table"],
        "komari_migration_primary_key": json.dumps(
            record["primary_key"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "komari_migration_content_sha256": record["content_sha256"],
        "komari_migration_manifest_sha256": manifest_sha256,
    }


def _manifest_bytes(manifest_path: Path) -> bytes:
    return manifest_path.read_bytes()


def _manifest_hash(manifest_path: Path) -> str:
    import hashlib

    return hashlib.sha256(_manifest_bytes(manifest_path)).hexdigest()


def _one_conversation_rows() -> dict[str, list[dict[str, Any]]]:
    return {
        CONVERSATIONS: [_conversation_row()],
        PROFILES: [],
        INTERACTIONS: [],
    }


def _set_target_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("HINDSIGHT_BASE_URL", "https://hindsight.example.test/")
    monkeypatch.setenv("HINDSIGHT_API_KEY", "secret-api-key")
    monkeypatch.setenv("HINDSIGHT_BANK_ID", "bank-1")


def _run_import(
    module: Any,
    monkeypatch: Any,
    manifest_path: Path,
    client: _FakeHindsightClient,
    *,
    clock: _FakeClock | None = None,
    progress_name: str = "progress.json",
    report_name: str = "report.json",
    extra_args: list[str] | None = None,
    set_env: bool = True,
) -> tuple[int, Path, Path, list[tuple[Any, float]]]:
    if set_env:
        _set_target_env(monkeypatch)
    progress_path = manifest_path.parent / progress_name
    report_path = manifest_path.parent / report_name
    creations: list[tuple[Any, float]] = []

    def _factory(target: Any, timeout: float) -> Any:
        creations.append((target, timeout))
        return client

    runtime = _runtime(module, client=_factory, clock=clock or _FakeClock())
    exit_code = asyncio.run(
        module.run_cli(
            [
                "import",
                "--apply",
                "--manifest",
                str(manifest_path),
                "--progress",
                str(progress_path),
                "--report",
                str(report_path),
                *(extra_args or []),
            ],
            boundaries=runtime,
        )
    )
    return exit_code, progress_path, report_path, creations


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def test_import_completed_operation_and_exact_document_succeeds(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_script_module()
    _code, manifest_path, _conn, _calls = _export(module, tmp_path, monkeypatch)
    client = _FakeHindsightClient()

    exit_code, progress_path, report_path, creations = _run_import(
        module, monkeypatch, manifest_path, client
    )

    assert exit_code == 0
    assert client.closed is True
    assert len(creations) == 1
    target, timeout = creations[0]
    assert target.base_url == "https://hindsight.example.test"
    assert target.api_key == "secret-api-key"
    assert target.bank_id == "bank-1"
    assert timeout == 30.0
    assert client.version_calls == 1
    assert len(client.retain_calls) == 3
    assert len(client.bank_profile_calls) == 1 + len(client.retain_calls)
    assert client.mutation_calls == []
    assert stat.S_IMODE(progress_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600

    progress = _read_json(progress_path)
    manifest = _read_json(manifest_path)
    assert progress["manifest_sha256"] == _manifest_hash(manifest_path)
    assert progress["target"] == {
        "base_url": "https://hindsight.example.test",
        "bank_id": "bank-1",
    }
    assert {entry["status"] for entry in progress["records"].values()} == {"completed"}
    for record in manifest["records"]:
        call = next(
            call
            for call in client.retain_calls
            if call["document_id"] == record["document_id"]
        )
        assert call["retain_async"] is True
        assert call["operation_id"] == str(uuid.UUID(call["operation_id"]))
        assert len(call["items"]) == 1
        assert call["items"][0]["content"] == record["source_text"]
        assert call["items"][0]["metadata"] == _expected_metadata(
            record,
            source_id=manifest["source_id"],
            manifest_sha256=progress["manifest_sha256"],
        )

    report = _read_json(report_path)
    assert report["outcome"] == "success"
    assert report["counts"] == {
        "completed": 3,
        "failed": 0,
        "unknown": 0,
        "conflict": 0,
    }
    assert [entry["status"] for entry in report["records"]] == [
        "completed",
        "completed",
        "completed",
    ]


def test_import_rejects_unsupported_api_version_without_writes(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_script_module()
    _code, manifest_path, _conn, _calls = _export(module, tmp_path, monkeypatch)
    client = _FakeHindsightClient(api_version="1.0.9")

    exit_code, progress_path, report_path, _creations = _run_import(
        module, monkeypatch, manifest_path, client
    )

    assert exit_code == 1
    assert client.closed is True
    assert client.retain_calls == []
    assert client.bank_profile_calls == []
    assert client.mutation_calls == []
    assert not progress_path.exists()
    report = _read_json(report_path)
    assert report["outcome"] == "failure"
    assert report["global_error_code"] == "target_version_unsupported"
    assert {entry["status"] for entry in report["records"]} == {"failed"}


def test_import_rejects_missing_bank_without_retain_or_creation(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_script_module()
    _code, manifest_path, _conn, _calls = _export(module, tmp_path, monkeypatch)
    client = _FakeHindsightClient(bank_error=_FakeApiError(404))

    exit_code, _progress_path, report_path, _creations = _run_import(
        module, monkeypatch, manifest_path, client
    )

    assert exit_code == 1
    assert client.retain_calls == []
    assert client.mutation_calls == []
    report = _read_json(report_path)
    assert report["global_error_code"] == "target_bank_missing"
    assert {entry["status"] for entry in report["records"]} == {"failed"}


@pytest.mark.parametrize(
    "profile",
    [{}, {"bank_id": "other-bank"}],
    ids=["missing", "mismatched"],
)
def test_import_rejects_invalid_bank_profile_identity_before_retain(
    monkeypatch: Any, tmp_path: Path, profile: dict[str, Any]
) -> None:
    module = _load_script_module()
    _code, manifest_path, _conn, _calls = _export(module, tmp_path, monkeypatch)
    client = _FakeHindsightClient(bank_profile_response=profile)

    exit_code, progress_path, report_path, _creations = _run_import(
        module, monkeypatch, manifest_path, client
    )

    assert exit_code == 1
    assert client.retain_calls == []
    assert client.operation_calls == []
    assert client.mutation_calls == []
    assert not progress_path.exists()
    report = _read_json(report_path)
    assert report["global_error_code"] == "target_bank_check_failed"
    assert {entry["status"] for entry in report["records"]} == {"failed"}


@pytest.mark.parametrize(
    "returned_operation_id",
    [None, "00000000-0000-4000-8000-000000000000"],
    ids=["missing", "mismatched"],
)
def test_import_rejects_invalid_operation_status_identity(
    monkeypatch: Any, tmp_path: Path, returned_operation_id: str | None
) -> None:
    module = _load_script_module()
    _code, manifest_path, _conn, _calls = _export(
        module,
        tmp_path,
        monkeypatch,
        connection=_FakeSourceConnection(_one_conversation_rows()),
    )
    client = _FakeHindsightClient(operation_response_operation_id=returned_operation_id)

    exit_code, progress_path, report_path, _creations = _run_import(
        module, monkeypatch, manifest_path, client
    )

    assert exit_code == 1
    assert len(client.retain_calls) == 1
    assert client.mutation_calls == []
    report = _read_json(report_path)
    assert [entry["status"] for entry in report["records"]] == ["unknown"]
    assert report["records"][0]["error_code"] == "operation_unknown"
    progress = _read_json(progress_path)
    entry = next(iter(progress["records"].values()))
    assert entry["status"] == "submitted"
    assert entry["last_error_code"] == "operation_unknown"


def test_import_pending_operation_never_reports_completion(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_script_module()
    _code, manifest_path, _conn, _calls = _export(
        module,
        tmp_path,
        monkeypatch,
        connection=_FakeSourceConnection(_one_conversation_rows()),
    )
    client = _FakeHindsightClient(default_operation_statuses=["pending"])
    clock = _FakeClock()

    exit_code, progress_path, report_path, _creations = _run_import(
        module,
        monkeypatch,
        manifest_path,
        client,
        clock=clock,
        extra_args=["--operation-timeout", "5", "--poll-interval", "2"],
    )

    assert exit_code == 1
    assert clock.sleeps == [2.0, 2.0]
    assert client.closed is True
    report = _read_json(report_path)
    assert report["outcome"] == "failure"
    assert [entry["status"] for entry in report["records"]] == ["unknown"]
    assert report["records"][0]["error_code"] == "operation_unknown"
    progress = _read_json(progress_path)
    entry = next(iter(progress["records"].values()))
    assert entry["status"] == "submitted"
    assert entry["last_error_code"] == "operation_unknown"


def test_import_poll_requests_use_request_timeout_not_operation_timeout(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_script_module()
    _code, manifest_path, _conn, _calls = _export(
        module,
        tmp_path,
        monkeypatch,
        connection=_FakeSourceConnection(_one_conversation_rows()),
    )
    client = _FakeHindsightClient(default_operation_statuses=["pending"])
    clock = _FakeClock()

    exit_code, _progress_path, report_path, _creations = _run_import(
        module,
        monkeypatch,
        manifest_path,
        client,
        clock=clock,
        extra_args=[
            "--request-timeout",
            "0.25",
            "--operation-timeout",
            "5",
            "--poll-interval",
            "1",
        ],
    )

    assert exit_code == 1
    assert client.operation_calls
    assert {timeout for _bank_id, _operation_id, timeout in client.operation_calls} == {
        0.25
    }
    report = _read_json(report_path)
    assert [entry["status"] for entry in report["records"]] == ["unknown"]
    assert report["records"][0]["error_code"] == "operation_unknown"


def test_import_bounds_slow_operation_status_request(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_script_module()
    _code, manifest_path, _conn, _calls = _export(
        module,
        tmp_path,
        monkeypatch,
        connection=_FakeSourceConnection(_one_conversation_rows()),
    )
    client = _FakeHindsightClient(operation_status_delay=1.0)
    clock = _FakeClock()

    exit_code, _progress_path, report_path, _creations = _run_import(
        module,
        monkeypatch,
        manifest_path,
        client,
        clock=clock,
        extra_args=[
            "--request-timeout",
            "0.1",
            "--operation-timeout",
            "5",
            "--poll-interval",
            "1",
        ],
    )

    assert exit_code == 1
    assert client.operation_status_delay_finished is False
    report = _read_json(report_path)
    assert [entry["status"] for entry in report["records"]] == ["unknown"]
    assert report["records"][0]["error_code"] == "operation_unknown"


def test_import_operation_deadline_spans_reconciliation_resubmission(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_script_module()
    _code, manifest_path, _conn, _calls = _export(
        module,
        tmp_path,
        monkeypatch,
        connection=_FakeSourceConnection(_one_conversation_rows()),
    )
    client = _FakeHindsightClient(
        default_operation_statuses=["pending", "not_found", "pending"]
    )
    clock = _FakeClock()

    exit_code, _progress_path, report_path, _creations = _run_import(
        module,
        monkeypatch,
        manifest_path,
        client,
        clock=clock,
        extra_args=[
            "--request-timeout",
            "1",
            "--operation-timeout",
            "3",
            "--poll-interval",
            "2",
        ],
    )

    assert exit_code == 1
    assert clock.sleeps == [2.0]
    assert len(client.retain_calls) == 2
    assert len({call["operation_id"] for call in client.retain_calls}) == 1
    report = _read_json(report_path)
    assert [entry["status"] for entry in report["records"]] == ["unknown"]
    assert report["records"][0]["error_code"] == "operation_unknown"


def test_import_late_completed_status_after_deadline_remains_unknown(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_script_module()
    _code, manifest_path, _conn, _calls = _export(
        module,
        tmp_path,
        monkeypatch,
        connection=_FakeSourceConnection(_one_conversation_rows()),
    )
    clock = _FakeClock()

    def _advance_past_deadline(_status: str) -> None:
        clock.now = 6.0

    client = _FakeHindsightClient(on_operation_status=_advance_past_deadline)

    exit_code, progress_path, report_path, _creations = _run_import(
        module,
        monkeypatch,
        manifest_path,
        client,
        clock=clock,
        extra_args=["--operation-timeout", "5", "--poll-interval", "1"],
    )

    assert exit_code == 1
    assert len(client.document_calls) == 1
    report = _read_json(report_path)
    assert [entry["status"] for entry in report["records"]] == ["unknown"]
    assert report["records"][0]["error_code"] == "operation_unknown"
    progress = _read_json(progress_path)
    entry = next(iter(progress["records"].values()))
    assert entry["status"] == "submitted"
    assert entry["operation_id"] == client.retain_calls[0]["operation_id"]


def test_import_operation_budget_bounds_not_found_reconciliation(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_script_module()
    _code, manifest_path, _conn, _calls = _export(
        module,
        tmp_path,
        monkeypatch,
        connection=_FakeSourceConnection(_one_conversation_rows()),
    )
    client = _FakeHindsightClient(
        default_operation_statuses=["not_found"],
        retain_creates_document=False,
        bank_profile_delay=1.0,
        bank_profile_delay_from_call=3,
    )
    clock = _FakeClock()

    exit_code, progress_path, report_path, _creations = _run_import(
        module,
        monkeypatch,
        manifest_path,
        client,
        clock=clock,
        extra_args=[
            "--request-timeout",
            "0.5",
            "--operation-timeout",
            "0.1",
            "--poll-interval",
            "0.05",
        ],
    )

    assert exit_code == 1
    assert len(client.bank_profile_calls) == 3
    assert client.bank_profile_delay_finished is False
    assert len(client.retain_calls) == 1
    report = _read_json(report_path)
    assert [entry["status"] for entry in report["records"]] == ["unknown"]
    assert report["records"][0]["error_code"] == "operation_unknown"
    progress = _read_json(progress_path)
    entry = next(iter(progress["records"].values()))
    assert entry["status"] == "submitted"
    assert entry["operation_id"] == client.retain_calls[0]["operation_id"]


def test_import_failed_and_unknown_operations_never_report_completion(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_script_module()
    _code, manifest_path, _conn, _calls = _export(
        module,
        tmp_path,
        monkeypatch,
        connection=_FakeSourceConnection(_one_conversation_rows()),
    )
    client = _FakeHindsightClient(default_operation_statuses=["failed"])

    exit_code, _progress_path, report_path, _creations = _run_import(
        module, monkeypatch, manifest_path, client
    )

    assert exit_code == 1
    report = _read_json(report_path)
    assert [entry["status"] for entry in report["records"]] == ["failed"]
    assert report["records"][0]["error_code"] == "operation_failed"


def test_import_receipt_only_is_not_completion(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_script_module()
    _code, manifest_path, _conn, _calls = _export(
        module,
        tmp_path,
        monkeypatch,
        connection=_FakeSourceConnection(_one_conversation_rows()),
    )
    client = _FakeHindsightClient(operation_error=_FakeApiError(500))
    clock = _FakeClock()

    exit_code, _progress_path, report_path, _creations = _run_import(
        module,
        monkeypatch,
        manifest_path,
        client,
        clock=clock,
        extra_args=["--operation-timeout", "3", "--poll-interval", "2"],
    )

    assert exit_code == 1
    assert client.closed is True
    assert len(client.retain_calls) == 1
    report = _read_json(report_path)
    assert [entry["status"] for entry in report["records"]] == ["unknown"]


def test_import_missing_original_text_fails(monkeypatch: Any, tmp_path: Path) -> None:
    module = _load_script_module()
    _code, manifest_path, _conn, _calls = _export(
        module,
        tmp_path,
        monkeypatch,
        connection=_FakeSourceConnection(_one_conversation_rows()),
    )
    client = _FakeHindsightClient(retain_document_text=None)

    exit_code, _progress_path, report_path, _creations = _run_import(
        module, monkeypatch, manifest_path, client
    )

    assert exit_code == 1
    assert client.mutation_calls == []
    report = _read_json(report_path)
    assert [entry["status"] for entry in report["records"]] == ["failed"]
    assert report["records"][0]["error_code"] == "target_document_text_missing"


def test_import_mismatched_original_text_fails(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_script_module()
    _code, manifest_path, _conn, _calls = _export(
        module,
        tmp_path,
        monkeypatch,
        connection=_FakeSourceConnection(_one_conversation_rows()),
    )
    client = _FakeHindsightClient(retain_document_text="different text")

    exit_code, _progress_path, report_path, _creations = _run_import(
        module, monkeypatch, manifest_path, client
    )

    assert exit_code == 1
    assert client.mutation_calls == []
    report = _read_json(report_path)
    assert report["records"][0]["error_code"] == "target_document_text_mismatch"


def test_import_conflicting_preexisting_target_never_receives_retain(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_script_module()
    _code, manifest_path, _conn, _calls = _export(
        module,
        tmp_path,
        monkeypatch,
        connection=_FakeSourceConnection(_one_conversation_rows()),
    )
    manifest = _read_json(manifest_path)
    record = manifest["records"][0]
    client = _FakeHindsightClient(
        documents={
            record["document_id"]: _document(
                document_id=record["document_id"],
                bank_id="bank-1",
                text=record["source_text"],
                metadata=None,
            )
        }
    )

    exit_code, _progress_path, report_path, _creations = _run_import(
        module, monkeypatch, manifest_path, client
    )

    assert exit_code == 1
    assert client.retain_calls == []
    assert client.operation_calls == []
    report = _read_json(report_path)
    assert [entry["status"] for entry in report["records"]] == ["conflict"]
    assert report["records"][0]["error_code"] == "target_document_conflict"


def test_import_resume_uses_the_same_operation_id(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_script_module()
    _code, manifest_path, _conn, _calls = _export(
        module,
        tmp_path,
        monkeypatch,
        connection=_FakeSourceConnection(_one_conversation_rows()),
    )
    first_client = _FakeHindsightClient(retain_error=_FakeApiError(None))
    first_code, progress_path, _report_path, _creations = _run_import(
        module,
        monkeypatch,
        manifest_path,
        first_client,
        report_name="first-report.json",
    )
    assert first_code == 1
    first_progress = _read_json(progress_path)
    first_entry = next(iter(first_progress["records"].values()))
    saved_operation_id = first_entry["operation_id"]
    assert first_entry["status"] == "uncertain"

    record = _read_json(manifest_path)["records"][0]
    second_client = _FakeHindsightClient(
        default_operation_statuses=["not_found", "completed"]
    )
    unrelated_document = _document(
        document_id="unrelated-document",
        bank_id="bank-1",
        text="unrelated text",
        metadata=None,
    )
    second_client.document_store["unrelated-document"] = unrelated_document
    second_code, _progress_path, second_report_path, _creations = _run_import(
        module,
        monkeypatch,
        manifest_path,
        second_client,
        report_name="second-report.json",
    )

    assert second_code == 0
    assert len(second_client.retain_calls) == 1
    assert second_client.retain_calls[0]["operation_id"] == saved_operation_id
    assert second_client.retain_calls[0]["document_id"] == record["document_id"]
    assert second_client.document_store["unrelated-document"] == unrelated_document
    assert {
        document_id for _bank_id, document_id, _timeout in second_client.document_calls
    } == {record["document_id"]}
    report = _read_json(second_report_path)
    assert [entry["status"] for entry in report["records"]] == ["completed"]


def test_import_rejects_changed_manifest_progress_binding(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_script_module()
    _code, manifest_path, _conn, _calls = _export(
        module,
        tmp_path,
        monkeypatch,
        connection=_FakeSourceConnection(_one_conversation_rows()),
    )
    first_client = _FakeHindsightClient(retain_error=_FakeApiError(None))
    _first_code, progress_path, _report_path, _creations = _run_import(
        module,
        monkeypatch,
        manifest_path,
        first_client,
        report_name="first-report.json",
    )
    assert progress_path.exists()
    manifest_path.write_bytes(manifest_path.read_bytes() + b"\n")
    second_client = _FakeHindsightClient()

    second_code, _progress_path, report_path, _creations = _run_import(
        module,
        monkeypatch,
        manifest_path,
        second_client,
        report_name="second-report.json",
    )

    assert second_code == 1
    assert second_client.retain_calls == []
    report = _read_json(report_path)
    assert report["global_error_code"] == "progress_binding_mismatch"


def test_import_rejects_changed_target_progress_binding(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_script_module()
    _code, manifest_path, _conn, _calls = _export(
        module,
        tmp_path,
        monkeypatch,
        connection=_FakeSourceConnection(_one_conversation_rows()),
    )
    first_client = _FakeHindsightClient(retain_error=_FakeApiError(None))
    _first_code, progress_path, _report_path, _creations = _run_import(
        module,
        monkeypatch,
        manifest_path,
        first_client,
        report_name="first-report.json",
    )
    progress = _read_json(progress_path)
    progress["target"]["bank_id"] = "other-bank"
    progress_path.write_text(json.dumps(progress), encoding="utf-8")
    second_client = _FakeHindsightClient()

    second_code, _progress_path, report_path, _creations = _run_import(
        module,
        monkeypatch,
        manifest_path,
        second_client,
        report_name="second-report.json",
    )

    assert second_code == 1
    assert second_client.retain_calls == []
    report = _read_json(report_path)
    assert report["global_error_code"] == "progress_binding_mismatch"


def test_import_rejects_concurrent_progress_lock(
    monkeypatch: Any, tmp_path: Path
) -> None:
    import fcntl

    module = _load_script_module()
    _code, manifest_path, _conn, _calls = _export(
        module,
        tmp_path,
        monkeypatch,
        connection=_FakeSourceConnection(_one_conversation_rows()),
    )
    progress_path = manifest_path.parent / "progress.json"
    lock_path = progress_path.with_name(progress_path.name + ".lock")
    lock_path.touch(mode=0o600)
    client = _FakeHindsightClient()

    with lock_path.open("a+") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        exit_code, _progress_path, report_path, creations = _run_import(
            module, monkeypatch, manifest_path, client
        )
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    assert exit_code == 1
    assert creations == []
    assert client.retain_calls == []
    assert not report_path.exists()
    assert module.ERROR_PROGRESS_LOCKED == "progress_locked"


def test_import_mixed_outcomes_reports_every_record_without_deletion(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_script_module()
    _code, manifest_path, _conn, _calls = _export(module, tmp_path, monkeypatch)
    manifest = _read_json(manifest_path)
    conflict_record = manifest["records"][0]
    failed_record = manifest["records"][1]
    client = _FakeHindsightClient(
        documents={
            conflict_record["document_id"]: _document(
                document_id=conflict_record["document_id"],
                bank_id="bank-1",
                text=conflict_record["source_text"],
                metadata=None,
            )
        },
        operation_statuses={failed_record["document_id"]: ["failed"]},
    )

    exit_code, _progress_path, report_path, _creations = _run_import(
        module, monkeypatch, manifest_path, client
    )

    assert exit_code == 1
    assert len(client.retain_calls) == 2
    report = _read_json(report_path)
    assert len(report["records"]) == 3
    statuses = [entry["status"] for entry in report["records"]]
    assert statuses.count("completed") == 1
    assert statuses.count("conflict") == 1
    assert statuses.count("failed") == 1
    assert client.mutation_calls == []
    assert {entry["document_id"] for entry in report["records"]} == {
        record["document_id"] for record in manifest["records"]
    }


def test_import_rejects_tampered_manifest_without_creating_client(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_script_module()
    _code, manifest_path, _conn, _calls = _export(
        module,
        tmp_path,
        monkeypatch,
        connection=_FakeSourceConnection(_one_conversation_rows()),
    )
    manifest = _read_json(manifest_path)
    manifest["records"][0]["source_text"] += "\ntampered"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    client = _FakeHindsightClient()

    exit_code, progress_path, report_path, creations = _run_import(
        module, monkeypatch, manifest_path, client
    )

    assert exit_code == 1
    assert creations == []
    assert client.retain_calls == []
    assert not progress_path.exists()
    report = _read_json(report_path)
    assert report["global_error_code"] == "manifest_invalid"


def test_import_requires_apply_and_target_environment(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_script_module()
    _code, manifest_path, _conn, _calls = _export(
        module,
        tmp_path,
        monkeypatch,
        connection=_FakeSourceConnection(_one_conversation_rows()),
    )
    client = _FakeHindsightClient()
    creations: list[tuple[Any, float]] = []

    def _factory(target: Any, timeout: float) -> Any:
        creations.append((target, timeout))
        return client

    with pytest.raises(SystemExit) as missing_apply:
        asyncio.run(
            module.run_cli(
                [
                    "import",
                    "--manifest",
                    str(manifest_path),
                    "--progress",
                    str(tmp_path / "progress.json"),
                    "--report",
                    str(tmp_path / "report.json"),
                ],
                boundaries=_runtime(module, client=_factory),
            )
        )
    assert missing_apply.value.code == 2
    assert creations == []

    monkeypatch.setenv("HINDSIGHT_BASE_URL", "https://hindsight.example.test")
    monkeypatch.delenv("HINDSIGHT_API_KEY", raising=False)
    monkeypatch.setenv("HINDSIGHT_BANK_ID", "bank-1")
    exit_code, progress_path, report_path, creations = _run_import(
        module,
        monkeypatch,
        manifest_path,
        client,
        set_env=False,
    )

    assert exit_code == 1
    assert creations == []
    assert not progress_path.exists()
    report = _read_json(report_path)
    assert report["global_error_code"] == "target_env_missing"


def test_script_does_not_import_the_bot_runtime(monkeypatch: Any) -> None:
    original_modules = dict(sys.modules)
    for name in list(sys.modules):
        if name == "komari_bot" or name.startswith("komari_bot."):
            monkeypatch.delitem(sys.modules, name, raising=False)

    module = _load_script_module()

    assert module.TABLE_CONVERSATIONS == CONVERSATIONS
    assert "komari_bot" not in sys.modules
    assert "hindsight_client" not in sys.modules

    sys.modules.clear()
    sys.modules.update(original_modules)


def test_import_persists_operation_id_before_first_network_write(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_script_module()
    _code, manifest_path, _conn, _calls = _export(
        module,
        tmp_path,
        monkeypatch,
        connection=_FakeSourceConnection(_one_conversation_rows()),
    )
    manifest = _read_json(manifest_path)
    record = manifest["records"][0]
    progress_path = tmp_path / "progress.json"
    observed: dict[str, Any] = {}

    def _on_retain(call: dict[str, Any]) -> None:
        progress = _read_json(progress_path)
        entry = progress["records"][record["document_id"]]
        observed.update(entry)
        observed["call_operation_id"] = call["operation_id"]
        observed["progress_text"] = progress_path.read_text(encoding="utf-8")

    client = _FakeHindsightClient(on_retain=_on_retain)
    exit_code, _progress_path, _report_path, _creations = _run_import(
        module, monkeypatch, manifest_path, client
    )

    assert exit_code == 0
    assert observed["call_operation_id"] == observed["operation_id"]
    assert observed["status"] == "pending_submit"
    manifest_sha256 = _manifest_hash(manifest_path)
    assert observed["request_identity"] == {
        "source_id": manifest["source_id"],
        "manifest_sha256": manifest_sha256,
        "table": record["table"],
        "primary_key": record["primary_key"],
        "document_id": record["document_id"],
        "content_sha256": record["content_sha256"],
    }
    assert "当前摘要" not in observed["progress_text"]
    assert "secret-api-key" not in observed["progress_text"]


def test_import_revalidates_completed_records_instead_of_trusting_progress(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_script_module()
    _code, manifest_path, _conn, _calls = _export(
        module,
        tmp_path,
        monkeypatch,
        connection=_FakeSourceConnection(_one_conversation_rows()),
    )
    first_client = _FakeHindsightClient()
    first_code, _progress_path, _report_path, _creations = _run_import(
        module,
        monkeypatch,
        manifest_path,
        first_client,
        report_name="first-report.json",
    )
    assert first_code == 0
    second_client = _FakeHindsightClient(
        default_operation_statuses=["failed"],
    )

    second_code, _progress_path, second_report_path, _creations = _run_import(
        module,
        monkeypatch,
        manifest_path,
        second_client,
        report_name="second-report.json",
    )

    assert second_code == 1
    assert second_client.retain_calls == []
    report = _read_json(second_report_path)
    assert [entry["status"] for entry in report["records"]] == ["failed"]
    assert report["records"][0]["error_code"] == "operation_failed"


def test_import_revalidates_completed_records_successfully_on_resume(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_script_module()
    _code, manifest_path, _conn, _calls = _export(
        module,
        tmp_path,
        monkeypatch,
        connection=_FakeSourceConnection(_one_conversation_rows()),
    )
    first_client = _FakeHindsightClient()
    first_code, _progress_path, _report_path, _creations = _run_import(
        module,
        monkeypatch,
        manifest_path,
        first_client,
        report_name="first-report.json",
    )
    assert first_code == 0
    manifest = _read_json(manifest_path)
    record = manifest["records"][0]
    second_client = _FakeHindsightClient(
        documents={
            record["document_id"]: _document(
                document_id=record["document_id"],
                bank_id="bank-1",
                text=record["source_text"],
                metadata=_expected_metadata(
                    record,
                    source_id=manifest["source_id"],
                    manifest_sha256=_manifest_hash(manifest_path),
                ),
            )
        }
    )

    second_code, _progress_path, second_report_path, _creations = _run_import(
        module,
        monkeypatch,
        manifest_path,
        second_client,
        report_name="second-report.json",
    )

    assert second_code == 0
    assert second_client.retain_calls == []
    report = _read_json(second_report_path)
    assert [entry["status"] for entry in report["records"]] == ["completed"]


def test_import_resume_conflicting_owned_document_never_resubmits(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_script_module()
    _code, manifest_path, _conn, _calls = _export(
        module,
        tmp_path,
        monkeypatch,
        connection=_FakeSourceConnection(_one_conversation_rows()),
    )
    first_client = _FakeHindsightClient(retain_error=_FakeApiError(None))
    first_code, _progress_path, _report_path, _creations = _run_import(
        module,
        monkeypatch,
        manifest_path,
        first_client,
        report_name="first-report.json",
    )
    assert first_code == 1
    record = _read_json(manifest_path)["records"][0]
    second_client = _FakeHindsightClient(
        documents={
            record["document_id"]: _document(
                document_id=record["document_id"],
                bank_id="bank-1",
                text="different text",
                metadata=None,
            )
        },
        default_operation_statuses=["not_found"],
    )

    second_code, _progress_path, second_report_path, _creations = _run_import(
        module,
        monkeypatch,
        manifest_path,
        second_client,
        report_name="second-report.json",
    )

    assert second_code == 1
    assert second_client.retain_calls == []
    report = _read_json(second_report_path)
    assert [entry["status"] for entry in report["records"]] == ["conflict"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"success": False},
        {"bank_id": "other-bank"},
        {"var_async": False},
        {"items_count": 2},
        {"operation_id": "00000000-0000-4000-8000-000000000000"},
    ],
    ids=["success", "bank", "async", "count", "operation-id"],
)
def test_import_invalid_receipts_never_report_completion(
    monkeypatch: Any,
    tmp_path: Path,
    overrides: dict[str, Any],
) -> None:
    module = _load_script_module()
    _code, manifest_path, _conn, _calls = _export(
        module,
        tmp_path,
        monkeypatch,
        connection=_FakeSourceConnection(_one_conversation_rows()),
    )
    client = _FakeHindsightClient(receipt_overrides=overrides)

    exit_code, progress_path, report_path, _creations = _run_import(
        module, monkeypatch, manifest_path, client
    )

    assert exit_code == 1
    report = _read_json(report_path)
    assert [entry["status"] for entry in report["records"]] == ["unknown"]
    assert report["records"][0]["error_code"] == "receipt_invalid"
    progress = _read_json(progress_path)
    entry = next(iter(progress["records"].values()))
    assert entry["status"] in {"uncertain", "submitted"}
    assert entry["last_error_code"] == "receipt_invalid"


def test_import_unknown_operation_status_never_reports_completion(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_script_module()
    _code, manifest_path, _conn, _calls = _export(
        module,
        tmp_path,
        monkeypatch,
        connection=_FakeSourceConnection(_one_conversation_rows()),
    )
    client = _FakeHindsightClient(default_operation_statuses=["running"])
    clock = _FakeClock()

    exit_code, _progress_path, report_path, _creations = _run_import(
        module,
        monkeypatch,
        manifest_path,
        client,
        clock=clock,
        extra_args=["--operation-timeout", "5", "--poll-interval", "2"],
    )

    assert exit_code == 1
    report = _read_json(report_path)
    assert [entry["status"] for entry in report["records"]] == ["unknown"]
    assert report["records"][0]["error_code"] == "operation_unknown"


def test_import_rejects_tampered_counts_without_creating_client(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_script_module()
    _code, manifest_path, _conn, _calls = _export(
        module,
        tmp_path,
        monkeypatch,
        connection=_FakeSourceConnection(_one_conversation_rows()),
    )
    manifest = _read_json(manifest_path)
    manifest["counts"][CONVERSATIONS] = 99
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    client = _FakeHindsightClient()

    exit_code, progress_path, report_path, creations = _run_import(
        module, monkeypatch, manifest_path, client
    )

    assert exit_code == 1
    assert creations == []
    assert client.retain_calls == []
    assert not progress_path.exists()
    report = _read_json(report_path)
    assert report["global_error_code"] == "manifest_invalid"


def test_import_private_files_never_contain_credentials_or_memory_text(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_script_module()
    _code, manifest_path, _conn, _calls = _export(module, tmp_path, monkeypatch)
    client = _FakeHindsightClient()

    exit_code, progress_path, report_path, _creations = _run_import(
        module, monkeypatch, manifest_path, client
    )

    assert exit_code == 0
    for path in (progress_path, report_path):
        text = path.read_text(encoding="utf-8")
        assert "secret-api-key" not in text
        assert "当前摘要" not in text
        assert "模糊事件" not in text
        assert "风格" not in text


def test_import_rejects_credentials_embedded_in_base_url(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_script_module()
    _code, manifest_path, _conn, _calls = _export(
        module,
        tmp_path,
        monkeypatch,
        connection=_FakeSourceConnection(_one_conversation_rows()),
    )
    client = _FakeHindsightClient()
    monkeypatch.setenv("HINDSIGHT_BASE_URL", "https://user:pass@hindsight.example.test")
    monkeypatch.setenv("HINDSIGHT_API_KEY", "secret-api-key")
    monkeypatch.setenv("HINDSIGHT_BANK_ID", "bank-1")

    exit_code, progress_path, report_path, creations = _run_import(
        module,
        monkeypatch,
        manifest_path,
        client,
        set_env=False,
    )

    assert exit_code == 1
    assert creations == []
    assert not progress_path.exists()
    report = _read_json(report_path)
    assert report["global_error_code"] == "target_config_invalid"


def test_export_rejects_dsn_like_source_id(monkeypatch: Any, tmp_path: Path) -> None:
    module = _load_script_module()
    output_path = tmp_path / "manifest.json"
    connect_calls: list[object] = []

    async def _connect(_dsn: str, _request_timeout: float) -> Any:
        connect_calls.append(object())
        raise AssertionError("source database must not be opened")  # noqa: TRY003

    monkeypatch.setenv(
        "SQLALCHEMY_DATABASE_URL",
        "postgresql+asyncpg://user:pass@localhost:5432/komari_bot",
    )
    exit_code = asyncio.run(
        module.run_cli(
            [
                "export",
                "--source-id",
                "postgresql://user:pass@host/db",
                "--output",
                str(output_path),
            ],
            boundaries=_runtime(module, connect=_connect),
        )
    )

    assert exit_code == 1
    assert connect_calls == []
    assert not output_path.exists()


def test_export_includes_all_rows_and_keeps_fuzzy_state(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_script_module()
    rows = {
        CONVERSATIONS: [
            _conversation_row(id=10, summary="later", is_fuzzy=True),
            _conversation_row(
                id=2,
                summary="earlier",
                is_fuzzy=False,
                dedup_key="dedup-2",
            ),
        ],
        PROFILES: [
            _profile_row(user_id="user-z"),
            _profile_row(user_id="user-a", group_id="group-200"),
        ],
        INTERACTIONS: [
            _interaction_row(id=20, event_summary="second", is_fuzzy=True),
            _interaction_row(id=3, event_summary="first", is_fuzzy=False),
        ],
    }

    exit_code, manifest_path, _conn, _calls = _export(
        module,
        tmp_path,
        monkeypatch,
        connection=_FakeSourceConnection(rows),
    )

    assert exit_code == 0
    manifest = _read_json(manifest_path)
    assert manifest["counts"] == {
        CONVERSATIONS: 2,
        PROFILES: 2,
        INTERACTIONS: 2,
    }
    conversations = [
        record for record in manifest["records"] if record["table"] == CONVERSATIONS
    ]
    assert [record["primary_key"]["id"] for record in conversations] == [2, 10]
    assert [record["fields"]["is_fuzzy"] for record in conversations] == [False, True]
    interactions = [
        record for record in manifest["records"] if record["table"] == INTERACTIONS
    ]
    assert [record["primary_key"]["id"] for record in interactions] == [3, 20]
    assert [record["fields"]["is_fuzzy"] for record in interactions] == [False, True]


def test_default_client_factory_uses_required_sdk_contract(monkeypatch: Any) -> None:
    module = _load_script_module()
    recorded: dict[str, Any] = {}

    class _FakeHindsight:
        def __init__(self, **kwargs: Any) -> None:
            recorded.update(kwargs)
            self.kwargs = kwargs

    monkeypatch.setattr(module, "_load_hindsight_client", lambda: _FakeHindsight)
    target = module.TargetConfig(
        base_url="https://hindsight.example.test",
        api_key="secret-api-key",
        bank_id="bank-1",
    )

    client = module._default_create_client(target, 12.5)

    assert isinstance(client, _FakeHindsight)
    assert recorded == {
        "base_url": "https://hindsight.example.test",
        "api_key": "secret-api-key",
        "timeout": 12.5,
        "max_attempts": 1,
    }


def test_export_real_postgres_disposable_database_covers_all_source_rows(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Run the real export against the explicitly provisioned TSK-191/196 DB.

    The test refuses to touch anything that is not a disposable
    ``tsk191_196_*`` database and deletes only its own uniquely marked rows.
    """

    import os

    import asyncpg

    dsn = os.environ.get("KOMARI_TEST_POSTGRES_URL", "")
    if not dsn:
        pytest.skip("disposable PostgreSQL URL is not configured")
    parts = url_split_for_test(dsn)
    database_name = parts.path.lstrip("/")
    if not database_name.startswith("tsk191_196_"):
        pytest.skip("configured database is not a TSK-191/196 disposable database")
    if parts.hostname not in {"127.0.0.1", "localhost", "::1"}:
        pytest.skip("disposable PostgreSQL must be on loopback")

    module = _load_script_module()
    marker = f"tsk196-{uuid.uuid4()}"
    manifest_path = tmp_path / "real-manifest.json"
    normalized_dsn = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)

    async def _run() -> tuple[dict[str, Any], dict[str, Any]]:
        connection = await asyncpg.connect(dsn=normalized_dsn)
        try:
            counts_before = {
                table: int(await connection.fetchval(f"SELECT COUNT(*) FROM {table}"))
                for table in (CONVERSATIONS, PROFILES, INTERACTIONS)
            }
            conversation_id = int(
                await connection.fetchval(
                    """
                    INSERT INTO komari_memory_conversations
                        (group_id, summary, participants, dedup_key,
                         start_time, end_time, importance, importance_initial,
                         importance_current, last_accessed, is_fuzzy, created_at)
                    VALUES ($1, $2, $3::text[], $4, $5, $6, 3, 2, 1, $5, TRUE, $5)
                    RETURNING id
                    """,
                    marker,
                    "真实数据库摘要 ✓",
                    ["real-user-a", "real-user-b"],
                    marker,
                    datetime(2026, 4, 5, 6, 7, 8),  # noqa: DTZ001
                    datetime(2026, 4, 5, 7, 8, 9),  # noqa: DTZ001
                )
            )
            await connection.execute(
                """
                INSERT INTO komari_memory_user_profile
                    (user_id, group_id, version, display_name, traits,
                     updated_at, importance, access_count, last_accessed)
                VALUES ($1, $2, 1, $3, $4::jsonb, $5, 5, 2, $6)
                """,
                f"{marker}-user",
                marker,
                "真实画像",
                json.dumps(
                    {"tone": "真实", "nested": {"flag": True}}, ensure_ascii=False
                ),
                datetime(2026, 4, 6, 7, 8, 9, tzinfo=UTC),
                datetime(2026, 4, 6, 7, 8, 9),  # noqa: DTZ001
            )
            interaction_id = int(
                await connection.fetchval(
                    """
                    INSERT INTO komari_memory_interaction_history
                        (user_id, display_name, event_summary, source_dedup_key,
                         source_message_count, first_seen_at, last_seen_at,
                         importance, importance_initial, importance_current,
                         last_accessed, is_fuzzy, created_at)
                    VALUES ($1, $2, $3, $4, 0, $5, $6, 4, 3, 5, $6, TRUE, $5)
                    RETURNING id
                    """,
                    f"{marker}-user",
                    "真实互动用户",
                    "真实数据库互动事件",
                    marker,
                    datetime(2026, 4, 7, 8, 9, 10, tzinfo=UTC),
                    datetime(2026, 4, 7, 9, 10, 11, tzinfo=UTC),
                )
            )

            async def _connect(real_dsn: str, request_timeout: float) -> Any:
                assert request_timeout == 60.0
                return await asyncpg.connect(
                    dsn=real_dsn,
                    command_timeout=request_timeout,
                )

            monkeypatch.setenv("SQLALCHEMY_DATABASE_URL", dsn)
            exit_code = await module.run_cli(
                [
                    "export",
                    "--source-id",
                    marker,
                    "--output",
                    str(manifest_path),
                ],
                boundaries=_runtime(module, connect=_connect),
            )
            if exit_code != 0:
                raise AssertionError(  # noqa: TRY003
                    f"real export failed with exit code {exit_code}: "
                    f"{manifest_path.parent.name}"
                )
            manifest = _read_json(manifest_path)
            counts_after = {
                table: int(await connection.fetchval(f"SELECT COUNT(*) FROM {table}"))
                for table in (CONVERSATIONS, PROFILES, INTERACTIONS)
            }
            return manifest, {
                **counts_before,
                "conversation_id": conversation_id,
                "interaction_id": interaction_id,
                "counts_after": counts_after,
            }
        finally:
            await connection.execute(
                "DELETE FROM komari_memory_conversations WHERE group_id = $1",
                marker,
            )
            await connection.execute(
                "DELETE FROM komari_memory_user_profile "
                "WHERE user_id = $1 AND group_id = $2",
                f"{marker}-user",
                marker,
            )
            await connection.execute(
                "DELETE FROM komari_memory_interaction_history "
                "WHERE user_id = $1 AND source_dedup_key = $2",
                f"{marker}-user",
                marker,
            )
            await connection.close()

    manifest, observed = asyncio.run(_run())
    assert manifest["counts"] == observed["counts_after"]
    assert manifest["counts"][CONVERSATIONS] == observed[CONVERSATIONS] + 1
    assert manifest["counts"][PROFILES] == observed[PROFILES] + 1
    assert manifest["counts"][INTERACTIONS] == observed[INTERACTIONS] + 1
    conversation = next(
        record
        for record in manifest["records"]
        if record["table"] == CONVERSATIONS
        and record["primary_key"]["id"] == observed["conversation_id"]
    )
    assert conversation["fields"]["summary"] == "真实数据库摘要 ✓"
    assert conversation["fields"]["participants"] == ["real-user-a", "real-user-b"]
    assert conversation["fields"]["is_fuzzy"] is True
    assert conversation["fields"]["importance_current"] == 1
    assert conversation["provenance"] == {"group_id": marker}

    profile = next(
        record
        for record in manifest["records"]
        if record["table"] == PROFILES
        and record["fields"]["user_id"] == f"{marker}-user"
    )
    assert profile["fields"]["traits"] == {
        "tone": "真实",
        "nested": {"flag": True},
    }
    assert profile["fields"]["group_id"] == marker

    interaction = next(
        record
        for record in manifest["records"]
        if record["table"] == INTERACTIONS
        and record["primary_key"]["id"] == observed["interaction_id"]
    )
    assert interaction["fields"]["event_summary"] == "真实数据库互动事件"
    assert interaction["fields"]["is_fuzzy"] is True
    assert interaction["provenance"] == {}

    assert "komari_memory_jobs" not in "\n".join(
        record["source_text"] for record in manifest["records"]
    )


def url_split_for_test(dsn: str) -> Any:
    from urllib.parse import urlsplit

    return urlsplit(dsn.replace("postgresql+asyncpg://", "postgresql://", 1))


def test_export_is_byte_deterministic_for_the_same_snapshot(
    monkeypatch: Any, tmp_path: Path
) -> None:
    import hashlib

    module = _load_script_module()
    rows = _default_rows()

    first_code, first_path, _conn, _calls = _export(
        module,
        tmp_path,
        monkeypatch,
        connection=_FakeSourceConnection(rows),
        output_name="first.json",
    )
    second_code, second_path, _conn, _calls = _export(
        module,
        tmp_path,
        monkeypatch,
        connection=_FakeSourceConnection(rows),
        output_name="second.json",
    )

    assert first_code == 0
    assert second_code == 0
    first_bytes = first_path.read_bytes()
    second_bytes = second_path.read_bytes()
    assert first_bytes == second_bytes
    manifest = json.loads(first_bytes)
    assert len(manifest["content_sha256"]) == 64
    for record in manifest["records"]:
        assert (
            record["content_sha256"]
            == hashlib.sha256(record["source_text"].encode("utf-8")).hexdigest()
        )


def test_import_rechecks_bank_before_each_new_submission(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_script_module()
    rows = {
        CONVERSATIONS: [
            _conversation_row(id=1, summary="first"),
            _conversation_row(id=2, summary="second", dedup_key="dedup-2"),
        ],
        PROFILES: [],
        INTERACTIONS: [],
    }
    _code, manifest_path, _conn, _calls = _export(
        module,
        tmp_path,
        monkeypatch,
        connection=_FakeSourceConnection(rows),
    )
    bank_calls_before_retain: list[int] = []
    client = _FakeHindsightClient(
        on_retain=lambda _call: bank_calls_before_retain.append(
            len(client.bank_profile_calls)
        )
    )

    exit_code, _progress_path, _report_path, _creations = _run_import(
        module, monkeypatch, manifest_path, client
    )

    assert exit_code == 0
    assert bank_calls_before_retain == [2, 3]
    assert len(client.retain_calls) == 2


@pytest.mark.parametrize(
    "metadata_case",
    ["retain-params-only", "mismatched"],
)
def test_import_rejects_missing_or_mismatching_document_metadata(
    monkeypatch: Any, tmp_path: Path, metadata_case: str
) -> None:
    module = _load_script_module()
    _code, manifest_path, _conn, _calls = _export(
        module,
        tmp_path,
        monkeypatch,
        connection=_FakeSourceConnection(_one_conversation_rows()),
    )
    first_client = _FakeHindsightClient()
    first_code, _progress_path, _report_path, _creations = _run_import(
        module,
        monkeypatch,
        manifest_path,
        first_client,
        report_name="first-report.json",
    )
    assert first_code == 0
    manifest = _read_json(manifest_path)
    record = manifest["records"][0]
    expected = _expected_metadata(
        record,
        source_id=manifest["source_id"],
        manifest_sha256=_manifest_hash(manifest_path),
    )
    if metadata_case == "retain-params-only":
        metadata = None
        retain_params = expected
    else:
        metadata = {**expected, "komari_migration_source_id": "other-source"}
        retain_params = None
    second_client = _FakeHindsightClient(
        documents={
            record["document_id"]: _document(
                document_id=record["document_id"],
                bank_id="bank-1",
                text=record["source_text"],
                metadata=metadata,
                retain_params=retain_params,
            )
        }
    )

    second_code, _progress_path, second_report_path, _creations = _run_import(
        module,
        monkeypatch,
        manifest_path,
        second_client,
        report_name="second-report.json",
    )

    assert second_code == 1
    assert second_client.retain_calls == []
    report = _read_json(second_report_path)
    assert [entry["status"] for entry in report["records"]] == ["failed"]
    assert report["records"][0]["error_code"] == "target_document_metadata_mismatch"


def test_import_rejects_non_finite_timeouts_before_creating_client(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_script_module()
    _code, manifest_path, _conn, _calls = _export(
        module,
        tmp_path,
        monkeypatch,
        connection=_FakeSourceConnection(_one_conversation_rows()),
    )
    client = _FakeHindsightClient()

    exit_code, progress_path, report_path, creations = _run_import(
        module,
        monkeypatch,
        manifest_path,
        client,
        extra_args=["--operation-timeout", "nan"],
    )

    assert exit_code == 1
    assert creations == []
    assert client.retain_calls == []
    assert not progress_path.exists()
    assert not report_path.exists()


def test_export_missing_source_table_fails_without_manifest(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    module = _load_script_module()
    rows = _default_rows()
    rows.pop(PROFILES)
    connection = _FakeSourceConnection(rows)
    output_path = tmp_path / "manifest.json"

    async def _connect(_dsn: str, _request_timeout: float) -> Any:
        return connection

    monkeypatch.setenv(
        "SQLALCHEMY_DATABASE_URL",
        "postgresql+asyncpg://user:pass@localhost:5432/komari_bot",
    )
    exit_code = asyncio.run(
        module.run_cli(
            [
                "export",
                "--source-id",
                "prod-deploy-1",
                "--output",
                str(output_path),
            ],
            boundaries=_runtime(module, connect=_connect),
        )
    )

    assert exit_code == 1
    assert not output_path.exists()
    assert connection.execute_calls == []
    captured = capsys.readouterr()
    assert module.ERROR_SOURCE_TABLE_MISSING in captured.err
    assert PROFILES in captured.err
    assert "当前摘要" not in captured.err


def test_import_rejects_missing_manifest_without_creating_client(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_script_module()
    client = _FakeHindsightClient()
    missing_manifest = tmp_path / "missing.json"
    progress_path = tmp_path / "progress.json"
    report_path = tmp_path / "report.json"
    creations: list[tuple[Any, float]] = []

    def _factory(target: Any, request_timeout: float) -> Any:
        creations.append((target, request_timeout))
        return client

    _set_target_env(monkeypatch)
    exit_code = asyncio.run(
        module.run_cli(
            [
                "import",
                "--apply",
                "--manifest",
                str(missing_manifest),
                "--progress",
                str(progress_path),
                "--report",
                str(report_path),
            ],
            boundaries=_runtime(module, client=_factory),
        )
    )

    assert exit_code == 1
    assert creations == []
    assert client.retain_calls == []
    assert not progress_path.exists()
    report = _read_json(report_path)
    assert report["global_error_code"] == "manifest_missing"
    assert report["records"] == []
