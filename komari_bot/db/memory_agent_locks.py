"""记忆 Agent 共享 PostgreSQL advisory lock。"""

from __future__ import annotations

import asyncio
import hashlib
import time
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import TYPE_CHECKING

from nonebot import logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import asyncpg


class MemoryAgentLockScope(StrEnum):
    """记忆 Agent 锁作用域。"""

    PROFILE_GROUP = "profile_group"
    INTERACTION_USER = "interaction_user"


@asynccontextmanager
async def acquire_memory_agent_lock(
    pg_pool: asyncpg.Pool,
    *,
    scope: MemoryAgentLockScope,
    group_id: str,
    user_id: str | None = None,
    trace_id: str,
    timeout_seconds: int | None = None,
) -> AsyncIterator[None]:
    """阻塞等待指定记忆 Agent 作用域锁，退出上下文时释放。"""
    lock_key = _build_lock_key(scope=scope, group_id=group_id, user_id=user_id)
    started = time.monotonic()
    logger.info(
        "[KomariMemory] 等待记忆 Agent 锁: trace_id={} scope={} group={} user={} key={}",
        trace_id,
        scope.value,
        group_id,
        user_id or "-",
        lock_key,
    )
    conn = await pg_pool.acquire()
    acquired = False
    try:
        if timeout_seconds is None:
            await conn.execute("SELECT pg_advisory_lock($1)", lock_key)
        else:
            await asyncio.wait_for(
                conn.execute("SELECT pg_advisory_lock($1)", lock_key),
                timeout=timeout_seconds,
            )
        acquired = True
        logger.info(
            "[KomariMemory] 已获取记忆 Agent 锁: trace_id={} scope={} group={} user={} wait_ms={:.0f}",
            trace_id,
            scope.value,
            group_id,
            user_id or "-",
            (time.monotonic() - started) * 1000,
        )
        yield
    finally:
        if acquired:
            await conn.execute("SELECT pg_advisory_unlock($1)", lock_key)
            logger.info(
                "[KomariMemory] 已释放记忆 Agent 锁: trace_id={} scope={} group={} user={}",
                trace_id,
                scope.value,
                group_id,
                user_id or "-",
            )
        await pg_pool.release(conn)


def _build_lock_key(
    *,
    scope: MemoryAgentLockScope,
    group_id: str,
    user_id: str | None,
) -> int:
    match scope:
        case MemoryAgentLockScope.PROFILE_GROUP:
            source = f"komari_memory:agent:profile:{group_id}"
        case MemoryAgentLockScope.INTERACTION_USER:
            if not user_id:
                msg = "INTERACTION_USER 锁必须提供 user_id"
                raise ValueError(msg)
            source = f"komari_memory:agent:interaction:{group_id}:{user_id}"
    digest = hashlib.blake2b(source.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)
