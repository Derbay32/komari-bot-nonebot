"""SceneRepository fingerprint、租约和状态收敛 SQL 测试。"""

from __future__ import annotations

import asyncio
from typing import Any, cast

from komari_bot.plugins.komari_decision.repositories.scene_repository import (
    SceneRepository,
)


class _FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb


class _FakeConnection:
    def __init__(self) -> None:
        self.fetchrow_results: list[dict[str, object] | None] = []
        self.fetch_results: list[list[dict[str, object]]] = []
        self.fetchval_results: list[object | None] = []
        self.fetchrow_calls: list[tuple[str, tuple[object, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[object, ...]]] = []
        self.fetchval_calls: list[tuple[str, tuple[object, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction()

    async def fetchrow(self, query: str, *args: object) -> dict[str, object] | None:
        self.fetchrow_calls.append((query, args))
        return self.fetchrow_results.pop(0)

    async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
        self.fetch_calls.append((query, args))
        return self.fetch_results.pop(0)

    async def fetchval(self, query: str, *args: object) -> object | None:
        self.fetchval_calls.append((query, args))
        return self.fetchval_results.pop(0)

    async def execute(self, query: str, *args: object) -> str:
        self.execute_calls.append((query, args))
        return "UPDATE 1"


class _FakeAcquire:
    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _FakeConnection:
        return self._connection

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb


class _FakePool:
    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._connection)


def _repository(connection: _FakeConnection) -> SceneRepository:
    return SceneRepository(cast("Any", _FakePool(connection)))


def test_get_or_create_scene_set_reuses_unique_fingerprint() -> None:
    connection = _FakeConnection()
    connection.fetchrow_results = [
        None,
        {
            "id": 9,
            "status": "BUILDING",
            "item_total": 2,
            "item_ready": 0,
            "item_failed": 0,
        },
    ]

    scene_set, created = asyncio.run(
        _repository(connection).get_or_create_scene_set(
            source_path="postgresql:komari_decision_scenes",
            source_hash="source-hash",
            embedding_model="model-x",
            embedding_instruction_hash="instruction-hash",
        )
    )

    assert created is False
    assert scene_set["id"] == 9
    insert_query, insert_args = connection.fetchrow_calls[0]
    assert "ON CONFLICT (" in insert_query
    assert "source_hash," in insert_query
    assert insert_args[1:] == (
        "source-hash",
        "model-x",
        "instruction-hash",
        "BUILDING",
    )


def test_claim_pending_items_uses_lease_and_skip_locked() -> None:
    connection = _FakeConnection()
    connection.fetch_results = [
        [
            {
                "id": 1,
                "status": "PROCESSING",
                "lease_owner": "owner-1",
                "attempt_count": 1,
            }
        ]
    ]

    items = asyncio.run(
        _repository(connection).claim_pending_items(
            10,
            owner_token="owner-1",
            limit=8,
            lease_seconds=90,
            max_attempts=4,
            retry_base_seconds=15,
        )
    )

    assert items[0]["lease_owner"] == "owner-1"
    reclaim_query, reclaim_args = connection.execute_calls[0]
    assert "lease_expires_at <= NOW()" in reclaim_query
    assert "last_error_code = 'lease_expired'" in reclaim_query
    assert reclaim_args == (10, 4, 15)
    claim_query, claim_args = connection.fetch_calls[0]
    assert "FOR UPDATE OF i SKIP LOCKED" in claim_query
    assert "status = 'PROCESSING'" in claim_query
    assert claim_args == (10, 8, "owner-1", 90)


def test_insert_scene_items_ignores_concurrent_duplicate_rows() -> None:
    connection = _FakeConnection()
    connection.fetchval_results = [101, None]
    repository = _repository(connection)
    item = {
        "scene_id": 5,
        "scene_key": "SCENE_TEST",
        "scene_type": "general",
        "content_text": "不可变场景文本",
        "enabled": True,
        "order_index": 1,
        "content_hash": "content-hash",
        "embedding": None,
        "embedding_dim": None,
        "status": "PENDING",
        "error_message": None,
        "embedded_at": None,
    }

    inserted = asyncio.run(repository.insert_scene_items(10, [item, item]))

    assert inserted == 1
    assert len(connection.fetchval_calls) == 2
    assert all(
        "ON CONFLICT (set_id, scene_id) DO NOTHING" in query
        for query, _args in connection.fetchval_calls
    )
    assert all(
        args[2:7] == (
            "SCENE_TEST",
            "general",
            "不可变场景文本",
            True,
            1,
        )
        for _query, args in connection.fetchval_calls
    )


def test_item_completion_rejects_stale_owner_and_schedules_retry() -> None:
    connection = _FakeConnection()
    connection.fetchval_results = [None, "PENDING"]
    repository = _repository(connection)

    ready = asyncio.run(
        repository.mark_item_ready(1, "old-owner", [0.1, 0.2], 2)
    )
    outcome = asyncio.run(
        repository.complete_item_failure(
            1,
            owner_token="owner-2",
            error_code="embedding_timeout",
            error_message="embedding 请求超时",
            max_attempts=3,
            retry_base_seconds=30,
        )
    )

    assert ready is False
    assert outcome == "pending"
    ready_query, ready_args = connection.fetchval_calls[0]
    assert "lease_owner = $4" in ready_query
    assert ready_args[-1] == "old-owner"
    failure_query, failure_args = connection.fetchval_calls[1]
    assert "lease_owner = $2" in failure_query
    assert "attempt_count >= $5" in failure_query
    assert failure_args == (
        1,
        "owner-2",
        "embedding 请求超时",
        "embedding_timeout",
        3,
        30,
    )


def test_refresh_set_progress_locks_set_and_transitions_atomically() -> None:
    connection = _FakeConnection()
    connection.fetchrow_results = [
        {"id": 10, "status": "BUILDING"},
        {"total": 2, "ready_count": 2, "failed_count": 0},
        {
            "id": 10,
            "status": "READY",
            "item_total": 2,
            "item_ready": 2,
            "item_failed": 0,
        },
    ]

    progress = asyncio.run(_repository(connection).refresh_set_progress(10))

    assert progress["status"] == "READY"
    assert progress["previous_status"] == "BUILDING"
    lock_query, _args = connection.fetchrow_calls[0]
    update_query, update_args = connection.fetchrow_calls[2]
    assert "FOR UPDATE" in lock_query
    assert "status = $5" in update_query
    assert update_args == (10, 2, 2, 0, "READY")


def test_active_set_reads_and_switches_lock_runtime_pointer() -> None:
    read_connection = _FakeConnection()
    read_connection.fetchrow_results = [
        {"id": 10, "status": "READY", "runtime_updated_at": "now"}
    ]
    active = asyncio.run(_repository(read_connection).get_active_set())

    assert active is not None
    assert active["id"] == 10
    assert "FOR SHARE OF r" in read_connection.fetchrow_calls[0][0]

    switch_connection = _FakeConnection()
    switch_connection.fetchrow_results = [{"id": 11, "status": "READY"}]
    switch_connection.fetchval_results = [10]
    asyncio.run(_repository(switch_connection).switch_active_set(11))

    assert "FOR UPDATE" in switch_connection.fetchrow_calls[0][0]
    assert "FOR UPDATE" in switch_connection.fetchval_calls[0][0]
    assert any(
        "SET active_set_id = $1" in query
        for query, _args in switch_connection.execute_calls
    )
