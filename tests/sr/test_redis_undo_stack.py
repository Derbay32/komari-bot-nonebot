"""SR Redis 撤销栈连接生命周期测试。"""

from __future__ import annotations

import asyncio
import json
from importlib import import_module
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from nonebug import App


class _ConfigManager:
    async def get_async(self) -> object:
        await asyncio.sleep(0)
        return SimpleNamespace(redis_db=7)


class _FakeRedis:
    def __init__(self) -> None:
        self.aclose_calls = 0

    async def aclose(self) -> None:
        self.aclose_calls += 1


class _UndoPipeline:
    def __init__(self, redis: "_UndoRedis") -> None:
        self._redis = redis
        self._operations: list[tuple[str, tuple[object, ...]]] = []

    def lpush(self, *args: object) -> "_UndoPipeline":
        self._operations.append(("lpush", args))
        return self

    def ltrim(self, *args: object) -> "_UndoPipeline":
        self._operations.append(("ltrim", args))
        return self

    def expire(self, *args: object) -> "_UndoPipeline":
        self._operations.append(("expire", args))
        return self

    async def execute(self) -> list[int]:
        results: list[int] = []
        for operation, args in self._operations:
            if operation == "lpush":
                key, value = args
                self._redis.values.setdefault(str(key), []).insert(0, str(value))
                results.append(1)
            elif operation == "ltrim":
                key, start, end = args
                assert isinstance(start, int)
                assert isinstance(end, int)
                values = self._redis.values.setdefault(str(key), [])
                self._redis.values[str(key)] = values[start : end + 1]
                results.append(1)
            else:
                key, ttl = args
                assert isinstance(ttl, int)
                self._redis.expirations[str(key)] = ttl
                results.append(1)
        return results


class _UndoRedis:
    def __init__(self) -> None:
        self.values: dict[str, list[str]] = {}
        self.expirations: dict[str, int] = {}

    def pipeline(self) -> _UndoPipeline:
        return _UndoPipeline(self)

    async def lindex(self, key: str, index: int) -> str | None:
        values = self.values.get(key, [])
        return values[index] if len(values) > index else None

    async def eval(
        self,
        script: str,
        _key_count: int,
        key: str,
        *args: str,
    ) -> int:
        values = self.values.get(key, [])
        if "LSET" in script:
            expected, replacement = args
            if values and values[0] == expected:
                values[0] = replacement
                return 1
            return 0

        token = args[0]
        if not values:
            return 0
        decoded = json.loads(values[0])
        if decoded.get("token") != token:
            return 0
        values.pop(0)
        return 1


class AddCommand:
    def __init__(self, item: str) -> None:
        self.item = item
        self.index: int | None = None


@pytest.fixture
def redis_module(app: App) -> Any:
    del app
    import_module("komari_bot.plugins.sr")
    return import_module("komari_bot.plugins.sr.redis_undo_stack")


@pytest.mark.asyncio
async def test_redis_client_uses_structured_connection_arguments_and_single_init(
    redis_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_clients: list[_FakeRedis] = []
    captured_kwargs: list[dict[str, object]] = []

    def _create_redis(**kwargs: object) -> _FakeRedis:
        captured_kwargs.append(kwargs)
        client = _FakeRedis()
        created_clients.append(client)
        return client

    monkeypatch.setattr(redis_module, "_redis_client", None)
    monkeypatch.setattr(redis_module, "_redis_client_lock", None)
    monkeypatch.setattr(redis_module, "_redis_client_lock_loop", None)
    monkeypatch.setattr(redis_module.aioredis, "Redis", _create_redis)
    monkeypatch.setattr(
        redis_module,
        "get_shared_database_config",
        lambda: SimpleNamespace(
            redis_host="redis.internal",
            redis_port=6380,
            redis_password="p@ss:/?#word",
        ),
    )

    first, second = await asyncio.gather(
        redis_module.get_redis_client(_ConfigManager()),
        redis_module.get_redis_client(_ConfigManager()),
    )

    assert first is second
    assert len(created_clients) == 1
    assert captured_kwargs == [
        {
            "host": "redis.internal",
            "port": 6380,
            "db": 7,
            "password": "p@ss:/?#word",
            "decode_responses": True,
            "encoding": "utf-8",
        }
    ]

    await redis_module.close_redis()

    assert created_clients[0].aclose_calls == 1
    assert redis_module._redis_client is None


@pytest.mark.asyncio
async def test_sr_shutdown_closes_undo_redis(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del app
    sr_module = import_module("komari_bot.plugins.sr")
    close_calls = 0

    async def _close() -> None:
        nonlocal close_calls
        close_calls += 1

    monkeypatch.setattr(sr_module, "close_redis", _close)

    await sr_module.on_shutdown()

    assert close_calls == 1


@pytest.mark.asyncio
async def test_undo_stack_peeks_then_conditionally_pops_by_token(
    redis_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _UndoRedis()

    async def _get_client(_manager: object) -> _UndoRedis:
        return client

    command = AddCommand("甲")
    monkeypatch.setattr(redis_module, "get_redis_client", _get_client)

    token = await redis_module.push_undo("user", command, object())
    peeked = await redis_module.peek_undo("user", object())

    assert peeked == {
        "token": token,
        "type": "AddCommand",
        "item": "甲",
        "index": None,
    }
    assert len(client.values["sr:undo:user"]) == 1
    assert not await redis_module.pop_undo_if_token("user", "wrong", object())
    assert len(client.values["sr:undo:user"]) == 1
    assert await redis_module.pop_undo_if_token("user", token, object())
    assert client.values["sr:undo:user"] == []


@pytest.mark.asyncio
async def test_peek_migrates_legacy_record_with_token(
    redis_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _UndoRedis()
    client.values["sr:undo:user"] = [
        json.dumps(
            {"type": "DeleteCommand", "item": "乙", "index": 2},
            ensure_ascii=False,
        )
    ]

    async def _get_client(_manager: object) -> _UndoRedis:
        return client

    monkeypatch.setattr(redis_module, "get_redis_client", _get_client)

    peeked = await redis_module.peek_undo("user", object())

    assert peeked is not None
    assert peeked["type"] == "DeleteCommand"
    assert peeked["item"] == "乙"
    assert peeked["index"] == 2
    assert peeked["token"]
    stored = json.loads(client.values["sr:undo:user"][0])
    assert stored["token"] == peeked["token"]
