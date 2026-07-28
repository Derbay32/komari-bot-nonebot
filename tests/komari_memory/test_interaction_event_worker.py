"""跨群互动事件 worker 的租约与幂等行为测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from komari_bot.plugins.komari_memory.handlers import (
    interaction_event_worker as worker_module,
)


def _record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "event": "用户分享了轻小说",
        "result": "小鞠认真回应",
        "emotion": "开心",
        "display_name": "阿明",
        "timestamp": 1.0,
    }
    record.update(overrides)
    return record


class _FakeRedis:
    def __init__(self, records: list[dict[str, object]]) -> None:
        self.records = records
        self.claim_calls: list[dict[str, object]] = []
        self.renew_calls: list[dict[str, object]] = []
        self.ack_calls: list[dict[str, str]] = []
        self.requeue_calls: list[dict[str, str]] = []
        self._claim_available = True

    async def claim_pending_interaction_summaries(
        self,
        *,
        owner_token: str,
        count: int,
        lease_seconds: int,
    ) -> list[str]:
        self.claim_calls.append(
            {
                "owner_token": owner_token,
                "count": count,
                "lease_seconds": lease_seconds,
            }
        )
        if self._claim_available:
            self._claim_available = False
            return ["u1"]
        self._claim_available = True
        return []

    async def renew_interaction_summary_lease(
        self,
        *,
        user_id: str,
        owner_token: str,
        lease_seconds: int,
    ) -> bool:
        self.renew_calls.append(
            {
                "user_id": user_id,
                "owner_token": owner_token,
                "lease_seconds": lease_seconds,
            }
        )
        return True

    async def snapshot_global_interactions(self, user_id: str, token: str) -> str:
        return f"processing:{user_id}:{token}"

    async def get_processing_global_interactions(
        self,
        processing_key: str,
    ) -> list[dict[str, object]]:
        del processing_key
        return list(self.records)

    async def ack_processing_global_interactions(
        self,
        *,
        user_id: str,
        owner_token: str,
        processing_key: str,
    ) -> bool:
        self.ack_calls.append(
            {
                "user_id": user_id,
                "owner_token": owner_token,
                "processing_key": processing_key,
            }
        )
        return True

    async def requeue_processing_global_interactions(
        self,
        *,
        user_id: str,
        owner_token: str,
        processing_key: str,
    ) -> bool:
        self.requeue_calls.append(
            {
                "user_id": user_id,
                "owner_token": owner_token,
                "processing_key": processing_key,
            }
        )
        return True


class _FakeMemory:
    def __init__(self) -> None:
        self.event_ids: dict[str, int] = {}
        self.insert_calls: list[dict[str, Any]] = []

    async def get_interaction_event_id_by_dedup_key(
        self,
        dedup_key: str,
    ) -> int | None:
        return self.event_ids.get(dedup_key)

    async def insert_interaction_event(self, **kwargs: Any) -> int:
        self.insert_calls.append(kwargs)
        event_id = len(self.insert_calls)
        self.event_ids[str(kwargs["dedup_key"])] = event_id
        return event_id


def _patch_config(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        worker_module,
        "get_config",
        lambda: SimpleNamespace(
            global_interaction_enabled=True,
            global_interaction_processing_lease_seconds=120,
        ),
    )


def test_duplicate_snapshot_is_stored_only_once(monkeypatch: Any) -> None:
    _patch_config(monkeypatch)
    redis = _FakeRedis([_record()])
    memory = _FakeMemory()
    summarize_calls: list[str] = []

    async def _summarize(**kwargs: Any) -> SimpleNamespace:
        summarize_calls.append(str(kwargs["user_id"]))
        return SimpleNamespace(event_summary="阿明分享轻小说并得到认真回应。", importance=4)

    monkeypatch.setattr(worker_module, "summarize_interaction_events", _summarize)

    asyncio.run(worker_module.interaction_event_worker_task(redis, memory))  # type: ignore[arg-type]
    asyncio.run(worker_module.interaction_event_worker_task(redis, memory))  # type: ignore[arg-type]

    assert summarize_calls == ["u1"]
    assert len(memory.insert_calls) == 1
    assert len(redis.ack_calls) == 2
    assert redis.requeue_calls == []
    assert all(call["lease_seconds"] == 120 for call in redis.claim_calls)
    assert all(call["count"] == 1 for call in redis.claim_calls)


def test_worker_requeues_snapshot_after_final_failure(
    monkeypatch: Any,
) -> None:
    _patch_config(monkeypatch)
    redis = _FakeRedis([_record()])
    memory = _FakeMemory()

    async def _fail(**_kwargs: Any) -> None:
        msg = "模拟总结失败"
        raise RuntimeError(msg)

    monkeypatch.setattr(worker_module, "_summarize_processing_key", _fail)

    asyncio.run(worker_module.interaction_event_worker_task(redis, memory))  # type: ignore[arg-type]

    assert redis.ack_calls == []
    assert len(redis.requeue_calls) == 1
    assert redis.requeue_calls[0]["processing_key"].startswith("processing:u1:")


def test_snapshot_dedup_key_is_independent_of_dictionary_key_order() -> None:
    first = _record()
    second = dict(reversed(list(first.items())))

    assert worker_module._build_snapshot_dedup_key(
        "u1", [first]
    ) == worker_module._build_snapshot_dedup_key("u1", [second])
    assert worker_module._build_snapshot_dedup_key(
        "u1", [first]
    ) != worker_module._build_snapshot_dedup_key("u2", [first])


def test_worker_renews_lease_during_slow_summary(monkeypatch: Any) -> None:
    _patch_config(monkeypatch)
    monkeypatch.setattr(worker_module, "_MAX_HEARTBEAT_INTERVAL_SECONDS", 0.001)
    redis = _FakeRedis([_record()])
    memory = _FakeMemory()

    async def _slow_summary(**_kwargs: Any) -> SimpleNamespace:
        await asyncio.sleep(0.01)
        return SimpleNamespace(event_summary="慢速总结", importance=4)

    monkeypatch.setattr(worker_module, "summarize_interaction_events", _slow_summary)

    asyncio.run(worker_module.interaction_event_worker_task(redis, memory))  # type: ignore[arg-type]

    assert redis.renew_calls
    assert redis.ack_calls


def test_worker_caps_snapshot_to_recent_record_window(monkeypatch: Any) -> None:
    _patch_config(monkeypatch)
    records = [_record(timestamp=float(index)) for index in range(205)]
    redis = _FakeRedis(records)
    memory = _FakeMemory()
    summarized_timestamps: list[float] = []

    async def _summarize(**kwargs: Any) -> SimpleNamespace:
        summarized_timestamps.extend(
            float(record["timestamp"]) for record in kwargs["records"]
        )
        return SimpleNamespace(event_summary="有界总结", importance=4)

    monkeypatch.setattr(worker_module, "summarize_interaction_events", _summarize)

    asyncio.run(worker_module.interaction_event_worker_task(redis, memory))  # type: ignore[arg-type]

    assert len(summarized_timestamps) == 200
    assert summarized_timestamps[0] == 5.0
    assert memory.insert_calls[0]["source_message_count"] == 200


def test_disabled_interaction_worker_is_registered_dormant_for_hot_enable(
    monkeypatch: Any,
) -> None:
    add_job_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    scheduler = SimpleNamespace(
        add_job=lambda *args, **kwargs: add_job_calls.append((args, kwargs))
    )
    monkeypatch.setattr(worker_module, "scheduler", scheduler)
    monkeypatch.setattr(
        worker_module,
        "get_config",
        lambda: SimpleNamespace(
            global_interaction_enabled=False,
            global_interaction_summary_interval_minutes=3,
        ),
    )

    worker_module.register_interaction_event_task(
        cast("Any", SimpleNamespace()),
        cast("Any", SimpleNamespace()),
    )

    assert len(add_job_calls) == 2
    interval_args, interval_kwargs = add_job_calls[0]
    assert interval_args[1] == "interval"
    assert interval_kwargs["minutes"] == 3
    assert interval_kwargs["id"] == worker_module._JOB_ID
