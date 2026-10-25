"""Neutral recent-context Redis operations and message schema.

The store consumes a supplied ``redis.asyncio.Redis`` client.  It never creates,
initializes, or closes that client; the komari_context plugin owns the client's
lifecycle.  Memory-specific processing/staging operations live in the
komari_memory specialization.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from nonebot import logger

from .redis_keys import RedisKeys

if TYPE_CHECKING:
    import redis.asyncio as aioredis

_CHAT_COMMIT_MESSAGE_ONCE_SCRIPT = """
-- chat_commit_message_once_v1
local dedupe_key = KEYS[1]
local buffer_key = KEYS[2]
local session_start_key = KEYS[3]
local last_message_key = KEYS[4]
local payload = ARGV[1]
local timestamp = ARGV[2]
local dedupe_ttl_seconds = tonumber(ARGV[3])

if redis.call("EXISTS", dedupe_key) == 1 then
    return 0
end
if redis.call("LLEN", buffer_key) == 0 then
    redis.call("SET", session_start_key, timestamp)
end
redis.call("RPUSH", buffer_key, payload)
redis.call("SET", last_message_key, timestamp)
if dedupe_ttl_seconds > 0 then
    redis.call("SET", dedupe_key, "1", "EX", dedupe_ttl_seconds)
else
    redis.call("SET", dedupe_key, "1")
end
return 1
"""
_CHAT_COMMIT_INTERACTION_ONCE_SCRIPT = """
-- chat_commit_interaction_once_v1
local dedupe_key = KEYS[1]
local interaction_key = KEYS[2]
local pending_key = KEYS[3]
local payload = ARGV[1]
local user_id = ARGV[2]
local trigger_size = tonumber(ARGV[3])
local dedupe_ttl_seconds = tonumber(ARGV[4])

if redis.call("EXISTS", dedupe_key) == 1 then
    return 0
end
redis.call("RPUSH", interaction_key, payload)
if redis.call("LLEN", interaction_key) >= trigger_size then
    redis.call("SADD", pending_key, user_id)
end
if dedupe_ttl_seconds > 0 then
    redis.call("SET", dedupe_key, "1", "EX", dedupe_ttl_seconds)
else
    redis.call("SET", dedupe_key, "1")
end
return 1
"""
_GLOBAL_INTERACTION_PUSH_SCRIPT = """
-- global_interaction_push_v1
local interaction_key = KEYS[1]
local pending_key = KEYS[2]
local user_id = ARGV[1]
local trigger_size = tonumber(ARGV[2])

for index = 3, #ARGV do
    redis.call("RPUSH", interaction_key, ARGV[index])
end
local buffer_length = redis.call("LLEN", interaction_key)
if buffer_length >= trigger_size then
    redis.call("SADD", pending_key, user_id)
end
return buffer_length
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


def _log_redis_json_error(*, context: str, key: str, raw_item: object) -> None:
    """记录可关联但不包含原始正文的 Redis 反序列化错误。"""
    raw_bytes = (
        raw_item
        if isinstance(raw_item, bytes)
        else str(raw_item).encode("utf-8", errors="replace")
    )
    logger.warning(
        "[KomariContext] Redis JSON 解析失败: "
        "context={} key={} bytes={} sha256={}",
        context,
        key,
        len(raw_bytes),
        hashlib.sha256(raw_bytes).hexdigest(),
    )


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
        """推入消息到缓冲区（连续追加，不再截断）。"""
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

        buffer_len = cast("int", await self.redis.execute_command("LLEN", key))
        now = time.time()
        pipe = self.redis.pipeline()
        if buffer_len == 0:
            pipe.set(RedisKeys.session_start(group_id), now)
        pipe.rpush(key, json.dumps(data))
        pipe.set(RedisKeys.last_message(group_id), now)
        await pipe.execute()

    async def push_message_once(
        self,
        group_id: str,
        message: MessageSchema,
        *,
        operation_id: str,
        dedupe_ttl_seconds: int | None = None,
    ) -> bool:
        """按聊天 operation ID 原子写入一次消息缓冲。

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
            4,
            RedisKeys.chat_commit_step(operation_id, "ai_history"),
            RedisKeys.buffer(group_id),
            RedisKeys.session_start(group_id),
            RedisKeys.last_message(group_id),
            json.dumps(data, ensure_ascii=False),
            message.timestamp,
            (max(1, dedupe_ttl_seconds) if dedupe_ttl_seconds is not None else 0),
        )
        return int(cast("int | str | bytes", result)) == 1

    async def get_buffer(
        self,
        group_id: str,
        limit: int = 100,
    ) -> list[MessageSchema]:
        """获取缓冲区消息。"""
        if limit <= 0:
            return []

        key = RedisKeys.buffer(group_id)
        # 读取尾部最近 N 条消息，Redis 会保持原有顺序返回。
        raw_data = await self.redis.lrange(key, -limit, -1)  # type: ignore[arg-type]

        return [self._deserialize_message(item) for item in raw_data]

    async def get_context_around_message(
        self,
        group_id: str,
        message_id: str,
        before: int = 5,
        after: int = 5,
    ) -> list[MessageSchema]:
        """获取指定消息前后的上下文。"""
        key = RedisKeys.buffer(group_id)
        raw_data = await self.redis.lrange(key, 0, -1)  # type: ignore[arg-type]
        all_messages = [self._deserialize_message(msg_item) for msg_item in raw_data]

        target_index = -1
        for i, message in enumerate(all_messages):
            if message.message_id == message_id:
                target_index = i
                break

        if target_index == -1:
            return []

        start = max(0, target_index - before)
        end = min(len(all_messages), target_index + after + 1)
        return all_messages[start:end]

    async def get_last_message_time(self, group_id: str) -> float | None:
        """获取群组最后一条消息的时间戳。"""
        value = await self.redis.get(RedisKeys.last_message(group_id))
        return float(value) if value else None

    async def get_session_start_time(self, group_id: str) -> float | None:
        """获取群组当前会话的开始时间戳。"""
        value = await self.redis.get(RedisKeys.session_start(group_id))
        return float(value) if value else None

    async def push_global_interaction(
        self,
        user_id: str,
        record: dict[str, Any] | list[dict[str, Any]],
        trigger_size: int = 20,
    ) -> None:
        """写入跨群用户互动缓冲，达到阈值后加入待总结集合。"""
        records = record if isinstance(record, list) else [record]
        if not records:
            return

        key = RedisKeys.global_interaction(user_id)
        payloads = [json.dumps(item, ensure_ascii=False) for item in records]
        await self.redis.execute_command(
            "EVAL",
            _GLOBAL_INTERACTION_PUSH_SCRIPT,
            2,
            key,
            RedisKeys.GLOBAL_INTERACTION_PENDING,
            user_id,
            max(1, trigger_size),
            *payloads,
        )

    async def push_global_interaction_once(
        self,
        *,
        user_id: str,
        record: dict[str, Any],
        trigger_size: int,
        operation_id: str,
        dedupe_ttl_seconds: int | None = None,
    ) -> bool:
        """按聊天 operation ID 原子写入一次互动事件缓冲。"""
        result = await self.redis.execute_command(
            "EVAL",
            _CHAT_COMMIT_INTERACTION_ONCE_SCRIPT,
            3,
            RedisKeys.chat_commit_step(operation_id, "interaction"),
            RedisKeys.global_interaction(user_id),
            RedisKeys.GLOBAL_INTERACTION_PENDING,
            json.dumps(record, ensure_ascii=False),
            user_id,
            max(1, trigger_size),
            (max(1, dedupe_ttl_seconds) if dedupe_ttl_seconds is not None else 0),
        )
        return int(cast("int | str | bytes", result)) == 1

    async def delete_chat_commit_evidence(self, operation_id: str) -> int:
        """幂等删除聊天履约的两个 Redis 防重证据键，返回删除数量。"""
        pipeline = self.redis.pipeline()
        pipeline.delete(RedisKeys.chat_commit_step(operation_id, "ai_history"))
        pipeline.delete(RedisKeys.chat_commit_step(operation_id, "interaction"))
        results = await pipeline.execute()
        return sum(int(result) for result in results)

    async def get_global_interaction_buffer(
        self,
        user_id: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """读取指定用户跨群互动原始缓冲尾部记录。"""
        if limit <= 0:
            return []

        key = RedisKeys.global_interaction(user_id)
        raw_items = await self.redis.lrange(key, -limit, -1)  # type: ignore[arg-type]
        records: list[dict[str, Any]] = []
        for raw_item in raw_items:
            try:
                parsed = json.loads(raw_item)
            except (TypeError, ValueError):
                _log_redis_json_error(
                    context="global_interaction_buffer",
                    key=key,
                    raw_item=raw_item,
                )
                continue
            if isinstance(parsed, dict):
                records.append(parsed)
        return records

    async def delete_buffer(self, group_id: str) -> None:
        """清空消息缓冲区及相关元数据。"""
        pipe = self.redis.pipeline()
        pipe.delete(RedisKeys.buffer(group_id))
        pipe.delete(RedisKeys.last_message(group_id))
        pipe.delete(RedisKeys.session_start(group_id))
        await pipe.execute()

    async def get_active_groups(self) -> list[str]:
        """获取有活跃消息缓冲的群组列表。"""
        pattern = RedisKeys.BUFFER_PATTERN
        keys = []
        dead_index_key = f"{RedisKeys.PREFIX}:buffer:processing_dead_index"
        excluded_keys = {dead_index_key}
        excluded_prefixes = (
            f"{RedisKeys.PREFIX}:buffer:processing:",
            f"{RedisKeys.PREFIX}:buffer:processing_current:",
            f"{RedisKeys.PREFIX}:buffer:processing_lock:",
            f"{RedisKeys.PREFIX}:buffer:processing_chunks:",
            f"{RedisKeys.PREFIX}:buffer:processing_meta:",
            f"{RedisKeys.PREFIX}:buffer:processing_dead:",
        )
        async for key in self.redis.scan_iter(match=pattern):
            key_text = self._decode_redis_text(key)
            if key_text in excluded_keys:
                continue
            if any(key_text.startswith(prefix) for prefix in excluded_prefixes):
                continue
            prefix = f"{RedisKeys.PREFIX}:buffer:"
            if not key_text.startswith(prefix):
                continue
            group_id = key_text.removeprefix(prefix)
            if group_id and ":" not in group_id:
                keys.append(group_id)
        return keys

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

    @staticmethod
    def _decode_redis_text(value: Any) -> str:
        if isinstance(value, bytes):
            return value.decode()
        return str(value)


__all__ = ["MessageSchema", "RecentContextStore"]
