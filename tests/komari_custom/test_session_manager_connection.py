"""komari_custom Redis 客户端连接边界测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from komari_bot.plugins.komari_custom import session_manager as session_manager_module
from komari_bot.plugins.komari_custom.session_manager import CustomSessionManager


def test_session_manager_uses_structured_password_and_single_initialization(
    monkeypatch: Any,
) -> None:
    created: list[dict[str, object]] = []
    closed: list[bool] = []

    class _ConfigManager:
        async def get_async(self) -> object:
            await asyncio.sleep(0)
            return SimpleNamespace(redis_db=7)

    class _Connection:
        async def aclose(self) -> None:
            closed.append(True)

    connection = _Connection()

    def _connection_factory(**kwargs: object) -> _Connection:
        created.append(kwargs)
        return connection

    monkeypatch.setattr(CustomSessionManager, "_redis_client", None)
    monkeypatch.setattr(CustomSessionManager, "_redis_client_lock", None)
    monkeypatch.setattr(CustomSessionManager, "_redis_client_lock_loop", None)
    monkeypatch.setattr(
        session_manager_module,
        "get_shared_database_config",
        lambda: SimpleNamespace(
            redis_host="redis.internal",
            redis_port=6380,
            redis_password="p@ss:/?#word",
        ),
    )
    monkeypatch.setattr(
        session_manager_module.aioredis,
        "Redis",
        _connection_factory,
    )
    first_manager = CustomSessionManager(_ConfigManager())
    second_manager = CustomSessionManager(_ConfigManager())

    async def _initialize_pair() -> tuple[object, object]:
        first, second = await asyncio.gather(
            first_manager._get_client(),
            second_manager._get_client(),
        )
        return first, second

    first, second = asyncio.run(_initialize_pair())

    assert first is connection
    assert second is connection
    assert created == [
        {
            "host": "redis.internal",
            "port": 6380,
            "db": 7,
            "password": "p@ss:/?#word",
            "decode_responses": True,
            "encoding": "utf-8",
        }
    ]

    asyncio.run(first_manager.close())
    assert closed == [True]
    assert CustomSessionManager._redis_client is None
