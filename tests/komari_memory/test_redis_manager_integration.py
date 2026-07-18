"""可选的真实 Redis owner lease 集成测试。"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, cast

import pytest
import redis.asyncio as aioredis

from komari_bot.plugins.komari_memory.config_schema import KomariMemoryConfigSchema
from komari_bot.plugins.komari_memory.services import (
    redis_manager as redis_manager_module,
)
from komari_bot.plugins.komari_memory.services.redis_keys import RedisKeys
from komari_bot.plugins.komari_memory.services.redis_manager import (
    MessageSchema,
    RedisManager,
)

REDIS_SOCKET = os.getenv("KOMARI_TEST_REDIS_SOCKET", "")
REDIS_URL = os.getenv("KOMARI_TEST_REDIS_URL", "")


def _redis_client() -> aioredis.Redis:
    if REDIS_URL:
        return aioredis.from_url(REDIS_URL, decode_responses=True)
    return aioredis.Redis(
        unix_socket_path=REDIS_SOCKET,
        decode_responses=True,
    )


@pytest.mark.skipif(
    not REDIS_SOCKET and not REDIS_URL,
    reason="未配置真实 Redis 测试连接",
)
def test_real_redis_conversation_owner_takeover(monkeypatch: Any) -> None:
    async def _run() -> None:
        config = KomariMemoryConfigSchema(
            conversation_processing_lease_seconds=30,
            conversation_snapshot_ttl_seconds=300,
        )
        monkeypatch.setattr(redis_manager_module, "get_config", lambda: config)
        first_client = _redis_client()
        second_client = _redis_client()
        await first_client.flushdb()
        await cast(
            "Any",
            first_client.rpush(
                RedisKeys.buffer("g1"),
                json.dumps(
                    {
                        "user_id": "u1",
                        "user_nickname": "用户一",
                        "group_id": "g1",
                        "content": "真实 Redis Lua 验证",
                        "timestamp": 1.0,
                        "message_id": "m1",
                        "is_bot": False,
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        first = RedisManager(config)
        second = RedisManager(config)
        first._redis = cast("Any", first_client)
        second._redis = cast("Any", second_client)
        try:
            first_claim = await first.claim_conversation_buffer(
                "g1", "owner-1", "snapshot-1"
            )
            busy_claim = await second.claim_conversation_buffer(
                "g1", "owner-2", "snapshot-2"
            )
            processing_key = str(first_claim.processing_key)
            await first.initialize_conversation_chunk_manifest(
                group_id="g1",
                processing_key=processing_key,
                owner_token="owner-1",
                manifest_json='{"version":1}',
            )
            await first_client.delete(RedisKeys.buffer_processing_lock("g1"))
            takeover = await second.claim_conversation_buffer(
                "g1", "owner-2", "snapshot-2"
            )

            assert first_claim.status == "claimed"
            assert busy_claim.status == "busy"
            assert takeover.status == "claimed"
            assert takeover.processing_key == processing_key
            assert not await first.ack_processing_conversation_buffer(
                "g1", processing_key, "owner-1"
            )
            assert not await first.restore_processing_conversation_buffer(
                "g1", processing_key, "owner-1"
            )
            assert (
                await second.get_conversation_chunk_state(
                    group_id="g1",
                    processing_key=processing_key,
                    owner_token="owner-2",
                    field="manifest",
                )
                == '{"version":1}'
            )
            messages = await second.get_processing_conversation_buffer(
                "g1", processing_key, "owner-2"
            )
            assert len(messages) == 1
            assert await second.ack_processing_conversation_buffer(
                "g1", processing_key, "owner-2"
            )
        finally:
            await first_client.flushdb()
            await first_client.aclose()
            await second_client.aclose()

    asyncio.run(_run())


@pytest.mark.skipif(
    not REDIS_SOCKET and not REDIS_URL,
    reason="未配置真实 Redis 测试连接",
)
def test_real_redis_conversation_dead_letter_requeue(monkeypatch: Any) -> None:
    async def _run() -> None:
        config = KomariMemoryConfigSchema(
            conversation_processing_lease_seconds=30,
            conversation_snapshot_ttl_seconds=300,
        )
        monkeypatch.setattr(redis_manager_module, "get_config", lambda: config)
        client = _redis_client()
        await client.flushdb()
        manager = RedisManager(config)
        manager._redis = cast("Any", client)
        old_message = {
            "user_id": "u1",
            "user_nickname": "用户一",
            "group_id": "g1",
            "content": "失败快照中的旧消息",
            "timestamp": 1.0,
            "message_id": "m1",
            "is_bot": False,
        }
        new_message = {
            **old_message,
            "content": "失败后到达的新消息",
            "timestamp": 2.0,
            "message_id": "m2",
        }
        try:
            await cast(
                "Any",
                client.rpush(
                    RedisKeys.buffer("g1"),
                    json.dumps(old_message, ensure_ascii=False),
                ),
            )
            await client.set(RedisKeys.last_message("g1"), "1.0")
            await client.set(RedisKeys.session_start("g1"), "1.0")
            claim = await manager.claim_conversation_buffer(
                "g1",
                "owner-1",
                "snapshot-dead",
            )
            processing_key = str(claim.processing_key)
            await manager.initialize_conversation_chunk_manifest(
                group_id="g1",
                processing_key=processing_key,
                owner_token="owner-1",
                manifest_json='{"version":1}',
            )

            assert await manager.dead_letter_processing_conversation_buffer(
                "g1",
                processing_key,
                "owner-1",
                failure_code="RuntimeError",
                attempt_count=3,
            )
            assert await manager.get_orphaned_conversation_processing_keys() == []
            dead_letters = await manager.list_conversation_dead_letters()
            assert len(dead_letters) == 1
            assert dead_letters[0].snapshot_id == "snapshot-dead"
            assert dead_letters[0].message_count == 1
            assert dead_letters[0].chunk_state_count == 1

            await cast(
                "Any",
                client.rpush(
                    RedisKeys.buffer("g1"),
                    json.dumps(new_message, ensure_ascii=False),
                ),
            )
            restored_count = await manager.requeue_conversation_dead_letter(
                group_id="g1",
                snapshot_id="snapshot-dead",
            )
            messages = await manager.get_buffer("g1")

            assert restored_count == 1
            assert [message.message_id for message in messages] == ["m1", "m2"]
            assert await manager.get_session_start_time("g1") == 1.0
            assert await manager.get_last_message_time("g1") == 2.0
            assert await manager.list_conversation_dead_letters() == []
        finally:
            await client.flushdb()
            await client.aclose()

    asyncio.run(_run())


@pytest.mark.skipif(
    not REDIS_SOCKET and not REDIS_URL,
    reason="未配置真实 Redis 测试连接",
)
def test_real_redis_chat_commit_steps_are_idempotent(monkeypatch: Any) -> None:
    async def _run() -> None:
        config = KomariMemoryConfigSchema()
        monkeypatch.setattr(redis_manager_module, "get_config", lambda: config)
        client = _redis_client()
        await client.flushdb()
        manager = RedisManager(config)
        manager._redis = cast("Any", client)
        try:
            assert (
                await manager.reserve_proactive_reply(
                    "g1",
                    "reservation-1",
                    max_per_hour=1,
                    reservation_ttl_seconds=30,
                )
                == "reserved"
            )
            assert await manager.renew_proactive_reply(
                "g1",
                "reservation-1",
                reservation_ttl_seconds=30,
            )

            message = MessageSchema(
                user_id="bot",
                user_nickname="小鞠",
                group_id="g1",
                content="真实 Redis 幂等回复",
                timestamp=12.5,
                message_id="bot-operation-1",
                is_bot=True,
            )
            assert await manager.push_message_once(
                "g1",
                message,
                operation_id="operation-1",
            )
            assert not await manager.push_message_once(
                "g1",
                message,
                operation_id="operation-1",
            )
            assert await manager.push_global_interaction_once(
                user_id="u1",
                record={"event": "发言", "result": "回复", "emotion": "平静"},
                trigger_size=1,
                operation_id="operation-1",
            )
            assert not await manager.push_global_interaction_once(
                user_id="u1",
                record={"event": "发言", "result": "回复", "emotion": "平静"},
                trigger_size=1,
                operation_id="operation-1",
            )

            assert len(await manager.get_buffer("g1")) == 1
            assert len(await manager.get_global_interaction_buffer("u1")) == 1
            assert await cast(
                "Any",
                client.sismember(RedisKeys.GLOBAL_INTERACTION_PENDING, "u1"),
            )
            assert await manager.claim_pending_interaction_summaries(
                owner_token="interaction-owner-1",
                count=1,
                lease_seconds=60,
            ) == ["u1"]
            assert not await manager.renew_interaction_summary_lease(
                user_id="u1",
                owner_token="interaction-owner-2",
                lease_seconds=60,
            )
            assert await manager.renew_interaction_summary_lease(
                user_id="u1",
                owner_token="interaction-owner-1",
                lease_seconds=60,
            )
            processing_key = await manager.snapshot_global_interactions(
                "u1",
                "integration-snapshot",
            )
            assert processing_key is not None
            assert await manager.ack_processing_global_interactions(
                user_id="u1",
                owner_token="interaction-owner-1",
                processing_key=processing_key,
            )
            await manager.push_global_interaction(
                user_id="u2",
                record=[
                    {"event": "第一次", "result": "回复", "emotion": "平静"},
                    {"event": "第二次", "result": "回复", "emotion": "平静"},
                ],
                trigger_size=2,
            )
            assert len(await manager.get_global_interaction_buffer("u2")) == 2
            assert await cast(
                "Any",
                client.sismember(RedisKeys.GLOBAL_INTERACTION_PENDING, "u2"),
            )
        finally:
            await client.flushdb()
            await client.aclose()

    asyncio.run(_run())
