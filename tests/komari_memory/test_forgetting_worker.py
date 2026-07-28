"""Forgetting worker tests."""

from __future__ import annotations

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


def test_unregister_clears_service_when_job_missing(monkeypatch: Any) -> None:
    scheduler = _FakeScheduler(JobLookupError("komari_memory_forgetting_worker"))
    manager = forgetting_worker.ForgettingTaskManager()

    monkeypatch.setattr(forgetting_worker, "scheduler", scheduler)

    manager.register(cast("Any", object()))
    manager.unregister()

    assert scheduler.removed_job_ids == ["komari_memory_forgetting_worker"]
    assert manager._service is None
