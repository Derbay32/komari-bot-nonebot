"""Forgetting worker tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from apscheduler.jobstores.base import JobLookupError

from komari_bot.plugins.komari_memory.handlers import forgetting_worker


class _FakeScheduler:
    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc
        self.removed_job_ids: list[str] = []

    def add_job(self, *_args: object, **_kwargs: object) -> None:
        return None

    def remove_job(self, job_id: str) -> None:
        self.removed_job_ids.append(job_id)
        if self.exc is not None:
            raise self.exc


class _FakeForgettingService:
    def __init__(self) -> None:
        self.decay_calls = 0

    async def decay_and_cleanup(self) -> None:
        self.decay_calls += 1


class _FakeRedisManager:
    def __init__(self) -> None:
        self.orphaned: list[tuple[str, str]] = []
        self.restore_calls: list[tuple[str, str]] = []
        self.fail_keys: set[str] = set()

    async def get_orphaned_conversation_processing_keys(self) -> list[tuple[str, str]]:
        return list(self.orphaned)

    async def claim_existing_conversation_processing(
        self,
        group_id: str,
        processing_key: str,
        owner_token: str,
    ) -> SimpleNamespace:
        del group_id, processing_key, owner_token
        return SimpleNamespace(status="claimed")

    async def restore_processing_conversation_buffer(
        self,
        group_id: str,
        processing_key: str,
        owner_token: str,
    ) -> bool:
        del owner_token
        self.restore_calls.append((group_id, processing_key))
        if processing_key in self.fail_keys:
            msg = "恢复失败"
            raise RuntimeError(msg)
        return True


def test_unregister_clears_service_when_job_missing(monkeypatch: Any) -> None:
    scheduler = _FakeScheduler(JobLookupError("komari_memory_forgetting_worker"))
    manager = forgetting_worker.ForgettingTaskManager()

    monkeypatch.setattr(forgetting_worker, "scheduler", scheduler)

    manager.register(cast("Any", object()))
    manager.unregister()

    assert scheduler.removed_job_ids == ["komari_memory_forgetting_worker"]
    assert manager._service is None
    assert manager._redis_manager is None


def test_execute_task_restores_orphaned_processing_keys(monkeypatch: Any) -> None:
    scheduler = _FakeScheduler()
    service = _FakeForgettingService()
    redis_manager = _FakeRedisManager()
    redis_manager.orphaned = [("g1", "processing-1"), ("g2", "processing-2")]
    redis_manager.fail_keys = {"processing-1"}
    manager = forgetting_worker.ForgettingTaskManager()

    monkeypatch.setattr(forgetting_worker, "scheduler", scheduler)

    manager.register(cast("Any", service), cast("Any", redis_manager))
    asyncio.run(manager._execute_task())

    assert service.decay_calls == 1
    assert redis_manager.restore_calls == [("g1", "processing-1"), ("g2", "processing-2")]
