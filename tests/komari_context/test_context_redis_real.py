"""TSK-197: bounded recent-context behavior against a real local Redis.

Covers the fixed capacity policy shared by ordinary appends and once-only
assistant history appends, ordered tails, read-only reads, persistent dedupe
evidence, duplicates after eviction, and cleanup of current plus historical
fulfillment evidence.  No live external service is used.
"""

from __future__ import annotations

import os
from typing import Any, cast
from uuid import uuid4

import pytest
import redis.asyncio as aioredis

from komari_bot.context.redis_keys import RedisKeys
from komari_bot.context.redis_store import (
    RECENT_CONTEXT_BUFFER_CAPACITY,
    MessageSchema,
    RecentContextStore,
)

REDIS_URL = os.getenv("KOMARI_TEST_REDIS_URL", "")
REDIS_SOCKET = os.getenv("KOMARI_TEST_REDIS_SOCKET", "")
REDIS_HOST = os.getenv("REDIS_HOST", "")
REDIS_PORT = os.getenv("REDIS_PORT", "")
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")
REDIS_DB = os.getenv("REDIS_DB", "")

REDIS_CONFIGURED = bool(
    REDIS_URL
    or REDIS_SOCKET
    or (REDIS_HOST and REDIS_PORT and REDIS_DB)
)

pytestmark = [
    pytest.mark.skipif(
        not REDIS_CONFIGURED,
        reason="未配置真实 Redis 测试连接（KOMARI_TEST_REDIS_URL 或 REDIS_HOST/PORT/DB）",
    ),
    pytest.mark.asyncio,
]


def _client() -> aioredis.Redis:
    if REDIS_URL:
        return aioredis.from_url(REDIS_URL, decode_responses=True)
    if REDIS_SOCKET:
        return aioredis.Redis(unix_socket_path=REDIS_SOCKET, decode_responses=True)
    return aioredis.Redis(
        host=REDIS_HOST,
        port=int(REDIS_PORT),
        db=int(REDIS_DB),
        password=REDIS_PASSWORD or None,
        decode_responses=True,
    )


def _message(group_id: str, index: int, *, is_bot: bool = False) -> MessageSchema:
    return MessageSchema(
        user_id="bot" if is_bot else f"user-{index}",
        user_nickname="小鞠" if is_bot else "用户",
        group_id=group_id,
        content=f"消息-{index}",
        timestamp=float(index),
        message_id=f"m-{index}",
        is_bot=is_bot,
    )


async def _cleanup(client: aioredis.Redis, group_id: str, **operations: str) -> None:
    keys = [RedisKeys.buffer(group_id)]
    keys.extend(
        RedisKeys.chat_commit_step(operation_id, step)
        for operation_id, step in operations.items()
    )
    await cast("Any", client.delete(*keys))



async def _assert_no_dead_metadata(client: aioredis.Redis, group_id: str) -> None:
    """New appends must not recreate the retired session metadata keys."""
    assert (
        await cast(
            "Any",
            client.exists(
                f"komari_memory:last_message:{group_id}",
                f"komari_memory:session_start:{group_id}",
            ),
        )
        == 0
    )


async def test_ordinary_append_keeps_newest_100_in_order() -> None:
    assert RECENT_CONTEXT_BUFFER_CAPACITY == 100
    client = _client()
    group_id = f"tsk197-ordinary-{uuid4().hex}"
    store = RecentContextStore(client)
    try:
        for index in range(105):
            await store.push_message(group_id, _message(group_id, index))

        assert await cast("Any", client.llen(RedisKeys.buffer(group_id))) == 100
        await _assert_no_dead_metadata(client, group_id)
        messages = await store.get_buffer(group_id)
        assert [message.message_id for message in messages] == [
            f"m-{index}" for index in range(5, 105)
        ]
        assert [message.timestamp for message in messages] == [
            float(index) for index in range(5, 105)
        ]
    finally:
        await _cleanup(client, group_id)
        await client.aclose()


async def test_once_only_append_keeps_newest_100_in_order() -> None:
    client = _client()
    group_id = f"tsk197-once-{uuid4().hex}"
    operation_prefix = uuid4().hex
    store = RecentContextStore(client)
    try:
        for index in range(103):
            accepted = await store.push_message_once(
                group_id,
                _message(group_id, index, is_bot=True),
                operation_id=f"{operation_prefix}-{index}",
            )
            assert accepted is True

        assert await cast("Any", client.llen(RedisKeys.buffer(group_id))) == 100
        await _assert_no_dead_metadata(client, group_id)
        messages = await store.get_buffer(group_id)
        assert [message.message_id for message in messages] == [
            f"m-{index}" for index in range(3, 103)
        ]
    finally:
        for index in range(103):
            await cast(
                "Any",
                client.delete(
                    RedisKeys.chat_commit_step(
                        f"{operation_prefix}-{index}", "ai_history"
                    )
                ),
            )
        await _cleanup(client, group_id)
        await client.aclose()


async def test_concurrent_ordinary_appends_remain_capped() -> None:
    import asyncio

    client = _client()
    group_id = f"tsk197-concurrent-{uuid4().hex}"
    store = RecentContextStore(client)
    try:
        await asyncio.gather(
            *(store.push_message(group_id, _message(group_id, index)) for index in range(250))
        )
        assert await cast("Any", client.llen(RedisKeys.buffer(group_id))) == 100
        messages = await store.get_buffer(group_id)
        assert len(messages) == 100
        assert len({message.message_id for message in messages}) == 100
    finally:
        await _cleanup(client, group_id)
        await client.aclose()


async def test_read_path_never_trims_or_writes() -> None:
    import json

    client = _client()
    group_id = f"tsk197-readonly-{uuid4().hex}"
    store = RecentContextStore(client)
    try:
        payloads = [
            json.dumps(
                {
                    "user_id": f"user-{index}",
                    "user_nickname": "用户",
                    "group_id": group_id,
                    "content": f"raw-{index}",
                    "timestamp": float(index),
                    "message_id": f"raw-{index}",
                    "is_bot": False,
                },
                ensure_ascii=False,
            )
            for index in range(120)
        ]
        await cast("Any", client.rpush(RedisKeys.buffer(group_id), *payloads))
        before = await cast("Any", client.lrange(RedisKeys.buffer(group_id), 0, -1))

        messages = await store.get_buffer(group_id)

        assert [message.message_id for message in messages] == [
            f"raw-{index}" for index in range(20, 120)
        ]
        assert await cast("Any", client.lrange(RedisKeys.buffer(group_id), 0, -1)) == before
    finally:
        await _cleanup(client, group_id)
        await client.aclose()


async def test_persistent_dedupe_survives_buffer_eviction_and_blocks_retry() -> None:
    client = _client()
    group_id = f"tsk197-dedupe-{uuid4().hex}"
    operation_id = uuid4().hex
    store = RecentContextStore(client)
    try:
        assistant = _message(group_id, 0, is_bot=True)
        assert (
            await store.push_message_once(
                group_id,
                assistant,
                operation_id=operation_id,
            )
            is True
        )
        dedupe_key = RedisKeys.chat_commit_step(operation_id, "ai_history")
        assert await cast("Any", client.exists(dedupe_key)) == 1
        assert await cast("Any", client.ttl(dedupe_key)) == -1

        for index in range(1, 105):
            await store.push_message(group_id, _message(group_id, index))

        assert assistant.message_id not in {
            message.message_id for message in await store.get_buffer(group_id)
        }
        assert (
            await store.push_message_once(
                group_id,
                assistant,
                operation_id=operation_id,
            )
            is False
        )
        assert await cast("Any", client.exists(dedupe_key)) == 1
        assert await cast("Any", client.llen(RedisKeys.buffer(group_id))) == 100

        assert await store.delete_chat_commit_evidence(operation_id) == 1
        assert await cast("Any", client.exists(dedupe_key)) == 0
    finally:
        await _cleanup(client, group_id, **{operation_id: "ai_history"})
        await client.aclose()


async def test_concurrent_duplicate_once_only_operation_appends_once() -> None:
    import asyncio

    client = _client()
    group_id = f"tsk197-once-duplicate-{uuid4().hex}"
    operation_id = uuid4().hex
    store = RecentContextStore(client)
    try:
        results = await asyncio.gather(
            *(
                store.push_message_once(
                    group_id,
                    _message(group_id, 0, is_bot=True),
                    operation_id=operation_id,
                )
                for _ in range(10)
            )
        )
        assert results.count(True) == 1
        assert await cast("Any", client.llen(RedisKeys.buffer(group_id))) == 1
    finally:
        await _cleanup(client, group_id, **{operation_id: "ai_history"})
        await client.aclose()


async def test_cleanup_removes_current_and_historical_evidence_keys() -> None:
    client = _client()
    operation_id = uuid4().hex
    current = RedisKeys.chat_commit_step(operation_id, "ai_history")
    historical = RedisKeys.chat_commit_step(operation_id, "interaction")
    store = RecentContextStore(client)
    try:
        await cast("Any", client.set(current, "1"))
        await cast("Any", client.set(historical, "1"))

        assert await store.delete_chat_commit_evidence(operation_id) == 2
        assert await cast("Any", client.exists(current, historical)) == 0
        assert await store.delete_chat_commit_evidence(operation_id) == 0
    finally:
        await cast("Any", client.delete(current, historical))
        await client.aclose()


async def test_evicted_assistant_message_is_not_reinserted_by_retry() -> None:
    client = _client()
    group_id = f"tsk197-evicted-{uuid4().hex}"
    operation_id = uuid4().hex
    store = RecentContextStore(client)
    try:
        assistant = _message(group_id, 0, is_bot=True)
        assert (
            await store.push_message_once(
                group_id, assistant, operation_id=operation_id
            )
            is True
        )
        for index in range(1, 101):
            await store.push_message(group_id, _message(group_id, index))

        assert (
            await store.push_message_once(
                group_id, assistant, operation_id=operation_id
            )
            is False
        )
        messages = await store.get_buffer(group_id)
        assert assistant.message_id not in {message.message_id for message in messages}
        assert len(messages) == RECENT_CONTEXT_BUFFER_CAPACITY
    finally:
        await _cleanup(client, group_id, **{operation_id: "ai_history"})
        await client.aclose()
