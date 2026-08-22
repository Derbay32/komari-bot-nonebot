"""TSK-230 记忆休眠真实存储门控验收（AC4 / AC9）。"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

REDIS_URL = os.getenv("KOMARI_TEST_REDIS_URL", "")
POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")
SQLALCHEMY_URL = os.getenv("SQLALCHEMY_DATABASE_URL", "")


def _db_identity(url: str) -> tuple[str, None | int, int]:
    parsed = urlsplit(url.replace("postgresql+asyncpg://", "postgresql://"))
    return parsed.hostname or "", parsed.port, len(parsed.path)


DATABASE_CONFIGURED = bool(POSTGRES_URL and SQLALCHEMY_URL) and (
    _db_identity(POSTGRES_URL) == _db_identity(SQLALCHEMY_URL)
)

pytestmark = pytest.mark.group_admission_service


def _processing_key(
    group_id: str, token: str
) -> str:
    from komari_bot.plugins.komari_memory.services.redis_keys import RedisKeys

    return RedisKeys.buffer_processing(group_id, token)


async def test_ac4_dormancy_freezes_snapshot_ttl_real_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC4（Redis 门控）：休眠期 TTL 冻结，快照安全进度不因 TTL 流逝消失。

    用真实 RedisManager 建立 processing 快照，休眠窗（> snapshot TTL）后快照
    必须仍存活（冻结转语义）。当前生产按 TTL 纯淘汰，恢复后进度丢失 → 红。
    """
    if not REDIS_URL:
        pytest.skip("未配置真实 Redis")
    import redis.asyncio as aioredis

    from komari_bot.plugins.komari_memory.services.redis_manager import (
        MessageSchema,
        RedisManager,
    )

    client = aioredis.from_url(REDIS_URL, decode_responses=True)
    group_id = f"tsk230-{uuid4().hex[:10]}"
    try:
        cfg = SimpleNamespace(
            conversation_processing_lease_seconds=60,
            conversation_snapshot_ttl_seconds=1,
        )
        from komari_bot.plugins.komari_memory.services import redis_manager as rm

        monkeypatch.setattr(rm, "get_config", lambda: cfg)
        mgr = RedisManager(cfg)  # type: ignore[arg-type] -- 真实连接配置简化
        mgr._redis = client
        await client.flushdb()
        msg = MessageSchema(
            user_id="u1",
            user_nickname="阿明",
            group_id=group_id,
            content="休眠 TTL 冻结验收",
            timestamp=1.0,
            message_id="m1",
        )
        await mgr.push_message(group_id, msg)
        claim = await mgr.claim_conversation_buffer(
            group_id, "owner", "tok"
        )
        assert claim.status == "claimed"
        key = _processing_key(group_id, "tok")
        assert await client.exists(key) == 1, "processing 快照应已建立"
        await asyncio.sleep(2.0)
        pttl = await client.pttl(key)
        assert pttl is not None and pttl > 0, "休眠期冻结快照 TTL 不得清零"
    finally:
        await client.flushdb()
        await client.aclose()


async def test_ac4_resume_restores_remaining_time_not_reset_to_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """补项2（AC4 恢复剩余时间）：休眠恢复只恢复剩余 TTL，不重新满额。

    用真实 RedisManager 建立 processing 快照，记录其存活 TTL；经
    ``claim_existing_conversation_processing`` 模拟休眠后恢复认领，恢复后的
    快照 TTL 不得比休眠前更长（复用剩余时间，「不重新满额」）。
    """
    if not REDIS_URL:
        pytest.skip("未配置真实 Redis")
    import redis.asyncio as aioredis

    from komari_bot.plugins.komari_memory.services.redis_manager import (
        MessageSchema,
        RedisManager,
    )

    client = aioredis.from_url(REDIS_URL, decode_responses=True)
    group_id = f"tsk230b-{uuid4().hex[:10]}"
    try:
        cfg = SimpleNamespace(
            conversation_processing_lease_seconds=1,
            conversation_snapshot_ttl_seconds=300,
        )
        from komari_bot.plugins.komari_memory.services import redis_manager as rm

        monkeypatch.setattr(rm, "get_config", lambda: cfg)
        mgr = RedisManager(cfg)  # type: ignore[arg-type] -- 真实连接配置简化
        mgr._redis = client
        await client.flushdb()
        msg = MessageSchema(
            user_id="u1",
            user_nickname="阿明",
            group_id=group_id,
            content="恢复剩余时间验收",
            timestamp=1.0,
            message_id="m1",
        )
        await mgr.push_message(group_id, msg)
        claim = await mgr.claim_conversation_buffer(group_id, "owner-a", "toka")
        assert claim.status == "claimed"
        key = _processing_key(group_id, "toka")
        # 休眠：短租约自然过期（claim_existing 只接管租约已死的孤儿快照，
        # 活跃租约按生产语义返回 busy），快照本体仍在冻结 TTL 内存活。
        await asyncio.sleep(1.2)
        before = await client.pttl(key)
        assert before is not None and before > 0, "休眠窗内快照应仍存活"
        # 休眠后由另一 owner 恢复接管同一快照（resume claim on existing）。
        resumed = await mgr.claim_existing_conversation_processing(
            group_id, key, "owner-b"
        )
        assert resumed.status == "claimed"
        after = await client.pttl(key)
        # 不重新满额：恢复后的剩余 TTL 不高于休眠前的存活 TTL。
        assert after is not None and after > 0
        assert after <= before, "恢复后不应把快照 TTL 重置得更满"
    finally:
        await client.flushdb()
        await client.aclose()


async def test_ac9_fresh_process_restores_real_redis_and_writes_no_lkg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC9（Redis 门控）：新进程从真实 Redis 恢复、不持久化 LKG。

    用两把互不共享内存的 RedisManager 模拟两个进程：进程 A 建快照，全新实例
    进程 B 读回同一快照（真实 Redis 是唯一精髓源），并断言 group_admission
    命名空间在恢复后没有任何键（LKG 不落任何第二存储）。
    """
    if not REDIS_URL:
        pytest.skip("未配置真实 Redis")
    import redis.asyncio as aioredis

    from komari_bot.plugins.komari_memory.services.redis_manager import (
        MessageSchema,
        RedisManager,
    )

    cfg = SimpleNamespace(
        conversation_processing_lease_seconds=60,
        conversation_snapshot_ttl_seconds=600,
    )
    from komari_bot.plugins.komari_memory.services import redis_manager as rm

    monkeypatch.setattr(rm, "get_config", lambda: cfg)
    client = aioredis.from_url(REDIS_URL, decode_responses=True)
    group_id = f"tsk230a-{uuid4().hex[:10]}"
    try:
        await client.flushdb()
        proc_a = RedisManager(cfg)  # type: ignore[arg-type] -- 真实连接配置简化
        proc_a._redis = client
        msg = MessageSchema(
            user_id="u1",
            user_nickname="阿明",
            group_id=group_id,
            content="AC9 恢复验收",
            timestamp=1.0,
            message_id="m1",
        )
        await proc_a.push_message(group_id, msg)
        claim = await proc_a.claim_conversation_buffer(group_id, "owner-a", "toka")
        assert claim.status == "claimed"
        key = _processing_key(group_id, "toka")

        # 进程 B：全新实例（无进程内状态），从 Redis 读回同一快照。
        proc_b = RedisManager(cfg)  # type: ignore[arg-type] -- 真实连接配置简化
        proc_b._redis = client
        got = await proc_b.get_processing_conversation_buffer(
            group_id, key, "owner-a"
        )
        assert len(got) == 1, "新进程必须从 Redis 恢复休眠中的安全进度"

        # LKG 只存进程内存，绝不写入第二存储（此处=Redis）。
        keys = list(await client.keys("*group_admission*"))
        assert keys == [], f"LKG 不得持久化到 Redis: {keys}"
    finally:
        await client.flushdb()
        await client.aclose()
