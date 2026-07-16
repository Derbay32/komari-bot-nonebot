"""SR Redis 撤销栈连接生命周期测试。"""

from __future__ import annotations

import asyncio
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
