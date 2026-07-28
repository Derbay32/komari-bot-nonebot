"""可选的真实 Redis Custom 会话 CAS 集成测试。"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from typing import Any, cast

import pytest
import redis.asyncio as aioredis

from komari_bot.plugins.komari_custom.session_manager import CustomSessionManager

REDIS_URL = os.getenv("KOMARI_TEST_REDIS_URL", "")


@pytest.mark.skipif(not REDIS_URL, reason="未配置真实 Redis 测试连接")
def test_real_redis_concurrent_appends_keep_both_updates(monkeypatch: Any) -> None:
    async def _run() -> None:
        client = aioredis.from_url(REDIS_URL, decode_responses=True)
        await client.flushdb()
        monkeypatch.setattr(CustomSessionManager, "_redis_client", client)
        config_manager = cast("Any", SimpleNamespace())
        first = CustomSessionManager(config_manager)
        second = CustomSessionManager(config_manager)
        try:
            await first.create_session(100, "200", title="原标题")
            await asyncio.gather(
                first.append_text(100, "200", "worker-A"),
                second.append_text(100, "200", "worker-B"),
            )

            saved = await first.get_session(100, "200")
            assert saved is not None
            assert set(saved.title.splitlines()) == {
                "原标题",
                "worker-A",
                "worker-B",
            }
            assert saved.version == 3
            assert len(saved.undo_stack) == 2
        finally:
            await client.flushdb()
            await client.aclose()

    asyncio.run(_run())
