"""RedisManager 缓冲区行为测试。"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

from komari_bot.plugins.komari_memory.config_schema import KomariMemoryConfigSchema
from komari_bot.plugins.komari_memory.services import (
    redis_manager as redis_manager_module,
)
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

    def rpush(self, key: str, value: str) -> "_FakePipeline":
        self._ops.append(("rpush", (key, value)))
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
                key, value = args
                self._redis.data.setdefault(str(key), []).append(str(value))
                results.append(len(self._redis.data[str(key)]))
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

    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self)

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        return _redis_range(self.data.get(key, []), start, stop)

    async def execute_command(self, command: str, key: str) -> int:
        assert command == "LLEN"
        return len(self.data.get(key, []))


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
