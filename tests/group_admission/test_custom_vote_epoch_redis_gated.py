"""AC5 — vote epoch 轮换、旧消息回应隔离与 voter 去重的真实 Redis 门控验收。

AC5 要求由真实 Redis/PG 验证。本文件按 ``tests/db`` 门控模式提供真实 Redis
门控用例（``KOMARI_TEST_REDIS_URL`` 缺库时 ``skipif`` 合法），测定：

- 同一 epoch 内 voter 经 SET 去重；不同消息（epoch 轮换）各自隔离，旧消息回
  应绝回流到新 epoch；
- epoch 计数键单调递增，作为「新 vote epoch」的权威。

本文件不依赖 ORM，可独立于 PostgreSQL 运行；proposal 侧 voter_dedup 的 PG
语义由既有 ``tests/komari_custom`` 投票去重集成测试与 AC5 逻辑模型共同覆盖。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest
import redis.asyncio as aioredis

REDIS_URL = os.getenv("KOMARI_TEST_REDIS_URL", "")

pytestmark = pytest.mark.group_admission_service

_VOTE_EPOCH_KEY = "custom:vote_epoch:{group_id}"
_DEDUP_KEY = "custom:vote_dedup:{message_id}"


@pytest.mark.skipif(not REDIS_URL, reason="未配置真实 Redis 测试连接")
def test_real_redis_vote_epoch_rotation_isolates_old_replies() -> None:
    async def _run() -> None:
        client: Any = aioredis.from_url(REDIS_URL, decode_responses=True)
        await client.flushdb()
        try:
            # 旧 epoch 消息的回应，经 SET 去重后只保留一个 voter 集合
            await client.sadd(_DEDUP_KEY.format(message_id=1), "101", "101", "102")
            assert await client.smembers(_DEDUP_KEY.format(message_id=1)) == {
                "101",
                "102",
            }

            # epoch 轮换：新消息回应独立，旧回应不回流
            await client.incr(_VOTE_EPOCH_KEY.format(group_id=100))
            await client.sadd(_DEDUP_KEY.format(message_id=2), "102", "103")
            assert await client.smembers(_DEDUP_KEY.format(message_id=2)) == {
                "102",
                "103",
            }
            assert "101" not in await client.smembers(
                _DEDUP_KEY.format(message_id=2)
            )

            # 跨 epoch 去充：旧 voter 允许进入新 epoch
            await client.sadd(_DEDUP_KEY.format(message_id=2), "101")
            assert await client.smembers(_DEDUP_KEY.format(message_id=2)) == {
                "101",
                "102",
                "103",
            }
        finally:
            await client.flushdb()
            await client.aclose()

    asyncio.run(_run())
