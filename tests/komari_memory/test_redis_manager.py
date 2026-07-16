"""RedisManager 缓冲区行为测试。"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

from komari_bot.plugins.komari_memory.config_schema import KomariMemoryConfigSchema
from komari_bot.plugins.komari_memory.services import (
    redis_manager as redis_manager_module,
)
from komari_bot.plugins.komari_memory.services.redis_keys import RedisKeys
from komari_bot.plugins.komari_memory.services.redis_manager import (
    MessageSchema,
    RedisManager,
)


def _redis_range(items: list[str], start: int, stop: int) -> list[str]:
    """模拟 Redis 的 LRANGE 索引语义。"""
    if not items:
        return []

    length = len(items)
    if start < 0:
        start += length
    if stop < 0:
        stop += length

    start = max(start, 0)
    stop = min(stop, length - 1)
    if start >= length or start > stop:
        return []
    return items[start : stop + 1]


class _FakePipeline:
    def __init__(self, redis: "_FakeRedis") -> None:
        self._redis = redis
        self._ops: list[tuple[str, tuple[Any, ...]]] = []

    def rpush(self, key: str, *values: str) -> "_FakePipeline":
        self._ops.append(("rpush", (key, *values)))
        return self

    def llen(self, key: str) -> "_FakePipeline":
        self._ops.append(("llen", (key,)))
        return self

    def set(self, key: str, value: object) -> "_FakePipeline":
        self._ops.append(("set", (key, value)))
        return self

    def delete(self, key: str) -> "_FakePipeline":
        self._ops.append(("delete", (key,)))
        return self

    async def execute(self) -> list[object]:
        results: list[object] = []
        for op, args in self._ops:
            if op == "rpush":
                key, *values = args
                self._redis.data.setdefault(str(key), []).extend(str(value) for value in values)
                results.append(len(self._redis.data[str(key)]))
            elif op == "llen":
                (key,) = args
                results.append(len(self._redis.data.get(str(key), [])))
            elif op == "set":
                key, value = args
                self._redis.values[str(key)] = str(value)
                results.append(True)
            elif op == "delete":
                (key,) = args
                self._redis.data.pop(str(key), None)
                self._redis.values.pop(str(key), None)
                results.append(1)
        return results


class _FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, list[str]] = {}
        self.values: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.now_ms = 1_000_000.0

    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self)

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        return _redis_range(self.data.get(key, []), start, stop)

    async def execute_command(self, command: str, *args: object) -> object:
        match command:
            case "LLEN":
                return self._llen(args)
            case "SADD":
                return self._sadd(args)
            case "SPOP":
                return self._spop(args)
            case "EVAL":
                return self._eval(args)
            case _:
                msg = f"未模拟 Redis 命令: {command}"
                raise AssertionError(msg)

    def _llen(self, args: tuple[object, ...]) -> int:
        (key,) = args
        return len(self.data.get(str(key), []))

    def _sadd(self, args: tuple[object, ...]) -> int:
        key, value = args
        self.sets.setdefault(str(key), set()).add(str(value))
        return 1

    def _spop(self, args: tuple[object, ...]) -> list[str]:
        key, count = args
        values = sorted(self.sets.get(str(key), set()))[: int(str(count))]
        self.sets.setdefault(str(key), set()).difference_update(values)
        return values

    def _eval(self, args: tuple[object, ...]) -> str | int | None:
        script, key_count, *rest = args
        if "proactive_reserve" in str(script):
            return self._eval_proactive_reserve(rest)
        if "proactive_confirm" in str(script):
            return self._eval_proactive_confirm(rest)
        if "proactive_release" in str(script):
            return self._eval_proactive_release(rest)
        if "proactive_count" in str(script):
            return self._eval_proactive_count(rest)

        assert int(str(key_count)) == 2
        key1 = str(rest[0])
        key2 = str(rest[1])
        if "RENAME" in str(script):
            return self._eval_snapshot(key1, key2)
        return self._eval_restore(key1, key2)

    def _prune_proactive_slots(self, slots_key: str, now_ms: float) -> None:
        slots = self.zsets.setdefault(slots_key, {})
        expired = [member for member, score in slots.items() if score <= now_ms]
        for member in expired:
            slots.pop(member, None)

    def _eval_proactive_reserve(self, rest: list[object]) -> int:
        cooldown_key, slots_key = map(str, rest[:2])
        reservation_id = str(rest[2])
        max_slots = int(str(rest[3]))
        reservation_ttl_ms = float(str(rest[4]))
        now_ms = self.now_ms
        pending_until_ms = now_ms + reservation_ttl_ms
        self._prune_proactive_slots(slots_key, now_ms)
        slots = self.zsets[slots_key]
        if (
            f"pending:{reservation_id}" in slots
            or f"confirmed:{reservation_id}" in slots
        ):
            return 3
        if cooldown_key in self.values:
            return 1
        if len(slots) >= max_slots:
            return 2
        slots[f"pending:{reservation_id}"] = pending_until_ms
        self.values[cooldown_key] = reservation_id
        return 0

    def _eval_proactive_confirm(self, rest: list[object]) -> int:
        cooldown_key, slots_key = map(str, rest[:2])
        reservation_id = str(rest[2])
        rate_window_ms = float(str(rest[5]))
        now_ms = self.now_ms
        confirmed_until_ms = now_ms + rate_window_ms
        self._prune_proactive_slots(slots_key, now_ms)
        slots = self.zsets[slots_key]
        confirmed_member = f"confirmed:{reservation_id}"
        if confirmed_member in slots:
            return 2
        had_pending = int(slots.pop(f"pending:{reservation_id}", None) is not None)
        slots[confirmed_member] = confirmed_until_ms
        current_cooldown = self.values.get(cooldown_key)
        if current_cooldown is None or current_cooldown == reservation_id:
            self.values[cooldown_key] = confirmed_member
        return had_pending

    def _eval_proactive_release(self, rest: list[object]) -> int:
        cooldown_key, slots_key = map(str, rest[:2])
        reservation_id = str(rest[2])
        slots = self.zsets.setdefault(slots_key, {})
        removed = int(slots.pop(f"pending:{reservation_id}", None) is not None)
        if self.values.get(cooldown_key) == reservation_id:
            self.values.pop(cooldown_key, None)
        return removed

    def _eval_proactive_count(self, rest: list[object]) -> int:
        slots_key = str(rest[0])
        self._prune_proactive_slots(slots_key, self.now_ms)
        return len(self.zsets[slots_key])

    def _eval_snapshot(self, source_key: str, processing_key: str) -> str | None:
        if not self.data.get(source_key):
            self.data.pop(source_key, None)
            return None
        assert processing_key not in self.data
        self.data[processing_key] = self.data.pop(source_key)
        return processing_key

    def _eval_restore(self, processing_key: str, target_key: str) -> int:
        old_items = list(self.data.get(processing_key, []))
        new_items = list(self.data.get(target_key, []))
        if not old_items:
            self.data.pop(processing_key, None)
            return 0
        self.data[target_key] = [*old_items, *new_items]
        self.data.pop(processing_key, None)
        return len(old_items)

    async def delete(self, key: str) -> int:
        existed = (
            key in self.data
            or key in self.values
            or key in self.sets
            or key in self.zsets
        )
        self.data.pop(key, None)
        self.values.pop(key, None)
        self.sets.pop(key, None)
        self.zsets.pop(key, None)
        return 1 if existed else 0

    async def exists(self, key: str) -> int:
        return (
            1
            if key in self.data
            or key in self.values
            or key in self.sets
            or key in self.zsets
            else 0
        )

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def scan_iter(self, *, match: str):
        prefix = match.removesuffix("*")
        for key in sorted(self.data):
            if key.startswith(prefix):
                yield key


def _build_message(index: int) -> MessageSchema:
    return MessageSchema(
        user_id=f"user-{index}",
        user_nickname=f"用户{index}",
        group_id="group-1",
        content=f"消息{index}",
        timestamp=float(index),
        message_id=f"msg-{index}",
    )


def _build_manager(monkeypatch: Any) -> RedisManager:
    config = KomariMemoryConfigSchema.model_construct()
    monkeypatch.setattr(
        redis_manager_module,
        "get_config",
        lambda: config,
    )
    manager = RedisManager(config)
    manager._redis = cast("Any", _FakeRedis())
    return manager


def _get_fake_redis(manager: RedisManager) -> _FakeRedis:
    return cast("_FakeRedis", manager._redis)


def test_proactive_reservation_is_atomic_for_concurrent_requests(
    monkeypatch: Any,
) -> None:
    manager = _build_manager(monkeypatch)

    async def _reserve_pair() -> list[str]:
        return list(
            await asyncio.gather(
                manager.reserve_proactive_reply(
                    "group-1",
                    "message-1",
                    max_per_hour=10,
                    reservation_ttl_seconds=360,
                ),
                manager.reserve_proactive_reply(
                    "group-1",
                    "message-2",
                    max_per_hour=10,
                    reservation_ttl_seconds=360,
                ),
            )
        )

    statuses = asyncio.run(_reserve_pair())

    assert statuses.count("reserved") == 1
    assert statuses.count("cooldown") == 1
    assert asyncio.run(manager.get_proactive_count("group-1")) == 1


def test_proactive_reservation_release_restores_capacity(monkeypatch: Any) -> None:
    manager = _build_manager(monkeypatch)

    first = asyncio.run(
        manager.reserve_proactive_reply(
            "group-1",
            "message-1",
            max_per_hour=1,
            reservation_ttl_seconds=360,
        )
    )
    released = asyncio.run(
        manager.release_proactive_reply("group-1", "message-1")
    )
    second = asyncio.run(
        manager.reserve_proactive_reply(
            "group-1",
            "message-2",
            max_per_hour=1,
            reservation_ttl_seconds=360,
        )
    )

    assert first == "reserved"
    assert released is True
    assert second == "reserved"
    assert asyncio.run(manager.get_proactive_count("group-1")) == 1


def test_confirmed_proactive_reservation_is_idempotent_and_rate_limited(
    monkeypatch: Any,
) -> None:
    manager = _build_manager(monkeypatch)
    fake_redis = _get_fake_redis(manager)

    assert (
        asyncio.run(
            manager.reserve_proactive_reply(
                "group-1",
                "message-1",
                max_per_hour=1,
                reservation_ttl_seconds=360,
            )
        )
        == "reserved"
    )
    asyncio.run(
        manager.confirm_proactive_reply(
            "group-1",
            "message-1",
            cooldown_seconds=300,
        )
    )
    asyncio.run(
        manager.confirm_proactive_reply(
            "group-1",
            "message-1",
            cooldown_seconds=300,
        )
    )
    assert asyncio.run(manager.release_proactive_reply("group-1", "message-1")) is False

    fake_redis.values.pop(RedisKeys.proactive_cooldown("group-1"), None)
    second = asyncio.run(
        manager.reserve_proactive_reply(
            "group-1",
            "message-2",
            max_per_hour=1,
            reservation_ttl_seconds=360,
        )
    )

    assert second == "rate_limited"
    assert asyncio.run(manager.get_proactive_count("group-1")) == 1


def test_expired_proactive_reservation_is_pruned(monkeypatch: Any) -> None:
    manager = _build_manager(monkeypatch)
    fake_redis = _get_fake_redis(manager)
    fake_redis.now_ms = 100_000.0

    assert (
        asyncio.run(
            manager.reserve_proactive_reply(
                "group-1",
                "message-1",
                max_per_hour=1,
                reservation_ttl_seconds=30,
            )
        )
        == "reserved"
    )
    fake_redis.values.pop(RedisKeys.proactive_cooldown("group-1"), None)
    fake_redis.now_ms = 131_000.0

    assert (
        asyncio.run(
            manager.reserve_proactive_reply(
                "group-1",
                "message-2",
                max_per_hour=1,
                reservation_ttl_seconds=30,
            )
        )
        == "reserved"
    )
    assert asyncio.run(manager.get_proactive_count("group-1")) == 1


def test_push_message_appends_messages_without_trimming(monkeypatch: Any) -> None:
    manager = _build_manager(monkeypatch)

    asyncio.run(manager.push_message("group-1", _build_message(1)))
    asyncio.run(manager.push_message("group-1", _build_message(2)))
    asyncio.run(manager.push_message("group-1", _build_message(3)))
    asyncio.run(manager.push_message("group-1", _build_message(4)))

    messages = asyncio.run(manager.get_buffer("group-1", limit=10))

    assert [msg.content for msg in messages] == ["消息1", "消息2", "消息3", "消息4"]

    fake_redis = _get_fake_redis(manager)
    assert redis_manager_module.RedisKeys.session_start("group-1") in fake_redis.values
    assert redis_manager_module.RedisKeys.last_message("group-1") in fake_redis.values


def test_get_buffer_returns_latest_window_in_time_order(monkeypatch: Any) -> None:
    manager = _build_manager(monkeypatch)
    key = redis_manager_module.RedisKeys.buffer("group-1")
    _get_fake_redis(manager).data[key] = [
        json.dumps(_build_message(index).__dict__, ensure_ascii=False)
        for index in range(1, 6)
    ]

    messages = asyncio.run(manager.get_buffer("group-1", limit=2))

    assert [msg.content for msg in messages] == ["消息4", "消息5"]


def test_push_global_interaction_triggers_pending_without_trimming(monkeypatch: Any) -> None:
    manager = _build_manager(monkeypatch)
    fake_redis = _get_fake_redis(manager)

    asyncio.run(
        manager.push_global_interaction(
            "u1",
            [{"event": f"事件{i}", "result": "回复", "emotion": "平静"} for i in range(21)],
            trigger_size=20,
        )
    )

    buffer_key = redis_manager_module.RedisKeys.global_interaction("u1")
    assert len(fake_redis.data[buffer_key]) == 21
    assert fake_redis.sets[redis_manager_module.RedisKeys.GLOBAL_INTERACTION_PENDING] == {
        "u1"
    }


def test_get_global_interaction_buffer_returns_tail_in_order(monkeypatch: Any) -> None:
    manager = _build_manager(monkeypatch)
    key = redis_manager_module.RedisKeys.global_interaction("u1")
    _get_fake_redis(manager).data[key] = [
        json.dumps({"event": f"事件{i}"}, ensure_ascii=False) for i in range(1, 5)
    ]

    records = asyncio.run(manager.get_global_interaction_buffer("u1", limit=2))

    assert records == [{"event": "事件3"}, {"event": "事件4"}]


def test_get_global_interaction_buffer_skips_invalid_json(monkeypatch: Any) -> None:
    manager = _build_manager(monkeypatch)
    key = redis_manager_module.RedisKeys.global_interaction("u1")
    _get_fake_redis(manager).data[key] = [
        "not-json",
        json.dumps({"event": "有效"}, ensure_ascii=False),
        json.dumps([{"event": "列表会跳过"}], ensure_ascii=False),
    ]

    records = asyncio.run(manager.get_global_interaction_buffer("u1", limit=10))

    assert records == [{"event": "有效"}]


def test_get_global_interaction_buffer_limit_zero_returns_empty(
    monkeypatch: Any,
) -> None:
    manager = _build_manager(monkeypatch)

    records = asyncio.run(manager.get_global_interaction_buffer("u1", limit=0))

    assert records == []


def test_global_interaction_snapshot_and_restore_keep_new_buffer_order(
    monkeypatch: Any,
) -> None:
    manager = _build_manager(monkeypatch)
    fake_redis = _get_fake_redis(manager)
    buffer_key = redis_manager_module.RedisKeys.global_interaction("u1")
    fake_redis.data[buffer_key] = ["old-1", "old-2"]

    processing_key = asyncio.run(manager.snapshot_global_interactions("u1", "token"))

    assert processing_key == redis_manager_module.RedisKeys.global_interaction_processing(
        "u1",
        "token",
    )
    assert buffer_key not in fake_redis.data
    assert fake_redis.data[str(processing_key)] == ["old-1", "old-2"]

    fake_redis.data[buffer_key] = ["new-1"]
    asyncio.run(manager.restore_processing_global_interactions("u1", str(processing_key)))

    assert fake_redis.data[buffer_key] == ["old-1", "old-2", "new-1"]
    assert str(processing_key) not in fake_redis.data


def test_get_users_with_global_interaction_buffer_excludes_pending_and_processing(
    monkeypatch: Any,
) -> None:
    manager = _build_manager(monkeypatch)
    fake_redis = _get_fake_redis(manager)
    fake_redis.data[redis_manager_module.RedisKeys.global_interaction("u1")] = ["record"]
    fake_redis.data[redis_manager_module.RedisKeys.global_interaction("u2")] = []
    fake_redis.data[redis_manager_module.RedisKeys.GLOBAL_INTERACTION_PENDING] = ["u3"]
    fake_redis.data[
        redis_manager_module.RedisKeys.global_interaction_processing("u4", "token")
    ] = ["record"]

    users = asyncio.run(manager.get_users_with_global_interaction_buffer())

    assert users == ["u1"]


def test_get_orphaned_conversation_processing_keys_filters_active_locks(
    monkeypatch: Any,
) -> None:
    manager = _build_manager(monkeypatch)
    fake_redis = _get_fake_redis(manager)
    active_key = redis_manager_module.RedisKeys.buffer_processing("g1", "token-active")
    no_lock_key = redis_manager_module.RedisKeys.buffer_processing("g2", "token-orphan")
    stale_lock_key = redis_manager_module.RedisKeys.buffer_processing("g3", "token-stale")
    invalid_key = f"{redis_manager_module.RedisKeys.PREFIX}:buffer:processing:broken"
    fake_redis.data[active_key] = ["record"]
    fake_redis.data[no_lock_key] = ["record"]
    fake_redis.data[stale_lock_key] = ["record"]
    fake_redis.data[invalid_key] = ["record"]
    fake_redis.values[redis_manager_module.RedisKeys.buffer_processing_lock("g1")] = active_key
    fake_redis.values[redis_manager_module.RedisKeys.buffer_processing_lock("g3")] = "other-key"

    orphaned = asyncio.run(manager.get_orphaned_conversation_processing_keys())

    assert orphaned == [("g2", no_lock_key), ("g3", stale_lock_key)]
