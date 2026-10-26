"""Neutral recent-context Redis operations and message schema.

The store consumes a supplied ``redis.asyncio.Redis`` client.  It never creates,
initializes, or closes that client; the komari_context plugin owns the client's
lifecycle.  Every append path atomically pushes and trims the group buffer to a
single bounded capacity, so the only live read path never depends on a removed
background drainer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from .redis_keys import RedisKeys

if TYPE_CHECKING:
    import redis.asyncio as aioredis

#: Maximum number of newest messages retained per group.
#:
#: One shared constant covers both ordinary appends and once-only assistant
#: history appends.  The current read path requests at most
#: ``context_messages_limit`` (maximum 50); the store keeps headroom without a
#: second retention policy.
RECENT_CONTEXT_BUFFER_CAPACITY = 100

_CHAT_COMMIT_MESSAGE_ONCE_SCRIPT = """
-- chat_commit_message_once_v3
local dedupe_key = KEYS[1]
local buffer_key = KEYS[2]
local payload = ARGV[1]
local dedupe_ttl_seconds = tonumber(ARGV[2])
local capacity = tonumber(ARGV[3])

if redis.call("EXISTS", dedupe_key) == 1 then
    return 0
end
redis.call("RPUSH", buffer_key, payload)
redis.call("LTRIM", buffer_key, -capacity, -1)
if dedupe_ttl_seconds > 0 then
    redis.call("SET", dedupe_key, "1", "EX", dedupe_ttl_seconds)
else
    redis.call("SET", dedupe_key, "1")
end
return 1
"""
_RECENT_CONTEXT_PUSH_SCRIPT = """
-- recent_context_push_v2
local buffer_key = KEYS[1]
local payload = ARGV[1]
local capacity = tonumber(ARGV[2])

redis.call("RPUSH", buffer_key, payload)
redis.call("LTRIM", buffer_key, -capacity, -1)
return 1
"""


@dataclass(frozen=True)
class MessageSchema:
    """消息数据结构。"""

    user_id: str
    user_nickname: str
    group_id: str
    content: str
    timestamp: float
    message_id: str
    is_bot: bool = False


class RecentContextStore:
    """Shared recent-context operations over an externally owned Redis client."""

    def __init__(self, redis: aioredis.Redis) -> None:
        self._redis = redis

    @property
    def redis(self) -> aioredis.Redis:
        """Return the supplied Redis client; ownership stays with the caller."""
        return self._redis

    async def push_message(
        self,
        group_id: str,
        message: MessageSchema,
    ) -> None:
        """原子追加消息并按共享容量保留最新记录。"""
        key = RedisKeys.buffer(group_id)
        data = {
            "user_id": message.user_id,
            "user_nickname": message.user_nickname,
            "group_id": message.group_id,
            "content": message.content,
            "timestamp": message.timestamp,
            "message_id": message.message_id,
            "is_bot": message.is_bot,
        }
        await self.redis.execute_command(
            "EVAL",
            _RECENT_CONTEXT_PUSH_SCRIPT,
            1,
            key,
            json.dumps(data, ensure_ascii=False),
            RECENT_CONTEXT_BUFFER_CAPACITY,
        )

    async def push_message_once(
        self,
        group_id: str,
        message: MessageSchema,
        *,
        operation_id: str,
        dedupe_ttl_seconds: int | None = None,
    ) -> bool:
        """按聊天 operation ID 原子写入一次消息缓冲并保留最新记录。

        ``dedupe_ttl_seconds`` 为 None 时防重键持久保留，直到履约终态
        清理显式删除；显式传秒数则沿用旧固定 TTL 语义（旧生产路径）。
        """
        data = {
            "user_id": message.user_id,
            "user_nickname": message.user_nickname,
            "group_id": message.group_id,
            "content": message.content,
            "timestamp": message.timestamp,
            "message_id": message.message_id,
            "is_bot": message.is_bot,
        }
        result = await self.redis.execute_command(
            "EVAL",
            _CHAT_COMMIT_MESSAGE_ONCE_SCRIPT,
            2,
            RedisKeys.chat_commit_step(operation_id, "ai_history"),
            RedisKeys.buffer(group_id),
            json.dumps(data, ensure_ascii=False),
            (max(1, dedupe_ttl_seconds) if dedupe_ttl_seconds is not None else 0),
            RECENT_CONTEXT_BUFFER_CAPACITY,
        )
        return int(cast("int | str | bytes", result)) == 1

    async def get_buffer(
        self,
        group_id: str,
        limit: int = RECENT_CONTEXT_BUFFER_CAPACITY,
    ) -> list[MessageSchema]:
        """读取缓冲区最新消息；读取路径不写入也不裁剪。"""
        if limit <= 0:
            return []

        key = RedisKeys.buffer(group_id)
        # 读取尾部最近 N 条消息，Redis 会保持原有顺序返回。
        raw_data = await self.redis.lrange(key, -limit, -1)  # type: ignore[arg-type]

        return [self._deserialize_message(item) for item in raw_data]

    async def delete_chat_commit_evidence(self, operation_id: str) -> int:
        """幂等删除聊天履约的两个 Redis 防重证据键，返回删除数量。"""
        pipeline = self.redis.pipeline()
        pipeline.delete(RedisKeys.chat_commit_step(operation_id, "ai_history"))
        pipeline.delete(RedisKeys.chat_commit_step(operation_id, "interaction"))
        results = await pipeline.execute()
        return sum(int(result) for result in results)

    @staticmethod
    def _deserialize_message(raw_item: str) -> MessageSchema:
        """将 Redis 中的 JSON 文本解析为消息对象。"""
        data = json.loads(raw_item)
        return MessageSchema(
            user_id=data["user_id"],
            user_nickname=data.get("user_nickname") or data["user_id"],
            group_id=data["group_id"],
            content=data["content"],
            timestamp=data["timestamp"],
            message_id=data["message_id"],
            is_bot=data.get("is_bot", False),
        )


__all__ = ["RECENT_CONTEXT_BUFFER_CAPACITY", "MessageSchema", "RecentContextStore"]
