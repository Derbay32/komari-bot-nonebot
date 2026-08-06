"""Komari Memory Redis 操作管理器。"""

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast

import redis.asyncio as aioredis
from nonebot import logger

from komari_bot.common.redis_config import get_shared_redis_config

from ..config_schema import KomariMemoryConfigSchema
from .config_interface import get_config
from .conversation_processing import (
    CONVERSATION_ACK_SCRIPT,
    CONVERSATION_CHUNK_LEDGER_SCRIPT,
    CONVERSATION_CLAIM_EXISTING_SCRIPT,
    CONVERSATION_CLAIM_SCRIPT,
    CONVERSATION_DEAD_LETTER_REQUEUE_SCRIPT,
    CONVERSATION_DEAD_LETTER_SCRIPT,
    CONVERSATION_GET_SCRIPT,
    CONVERSATION_RENEW_SCRIPT,
    CONVERSATION_RESTORE_SCRIPT,
    ConversationDeadLetter,
    ConversationLeaseLostError,
    ConversationSnapshotClaim,
)
from .redis_keys import RedisKeys

ProactiveReservationStatus = Literal[
    "reserved",
    "cooldown",
    "rate_limited",
    "duplicate",
]

_PROACTIVE_RATE_WINDOW_MS = 3_600_000
_PROACTIVE_SLOTS_TTL_GRACE_MS = 60_000
_PROACTIVE_RESERVE_SCRIPT = """
-- proactive_reserve
local cooldown_key = KEYS[1]
local slots_key = KEYS[2]
local reservation_id = ARGV[1]
local max_slots = tonumber(ARGV[2])
local reservation_ttl_ms = tonumber(ARGV[3])
local slots_ttl_ms = tonumber(ARGV[4])
local pending_member = "pending:" .. reservation_id
local confirmed_member = "confirmed:" .. reservation_id
local redis_time = redis.call("TIME")
local now_ms = tonumber(redis_time[1]) * 1000
    + math.floor(tonumber(redis_time[2]) / 1000)
local pending_until_ms = now_ms + reservation_ttl_ms

redis.call("ZREMRANGEBYSCORE", slots_key, "-inf", now_ms)
if redis.call("ZSCORE", slots_key, pending_member)
    or redis.call("ZSCORE", slots_key, confirmed_member) then
    return 3
end
if redis.call("EXISTS", cooldown_key) == 1 then
    return 1
end
if redis.call("ZCARD", slots_key) >= max_slots then
    return 2
end

redis.call("ZADD", slots_key, pending_until_ms, pending_member)
redis.call("PEXPIRE", slots_key, slots_ttl_ms)
redis.call("SET", cooldown_key, reservation_id, "PX", reservation_ttl_ms)
return 0
"""
_PROACTIVE_CONFIRM_SCRIPT = """
-- proactive_confirm
local cooldown_key = KEYS[1]
local slots_key = KEYS[2]
local reservation_id = ARGV[1]
local cooldown_ttl_ms = tonumber(ARGV[2])
local slots_ttl_ms = tonumber(ARGV[3])
local rate_window_ms = tonumber(ARGV[4])
local pending_member = "pending:" .. reservation_id
local confirmed_member = "confirmed:" .. reservation_id
local redis_time = redis.call("TIME")
local now_ms = tonumber(redis_time[1]) * 1000
    + math.floor(tonumber(redis_time[2]) / 1000)
local confirmed_until_ms = now_ms + rate_window_ms

redis.call("ZREMRANGEBYSCORE", slots_key, "-inf", now_ms)
if redis.call("ZSCORE", slots_key, confirmed_member) then
    return 2
end

local had_pending = redis.call("ZREM", slots_key, pending_member)
redis.call("ZADD", slots_key, confirmed_until_ms, confirmed_member)
redis.call("PEXPIRE", slots_key, slots_ttl_ms)

local current_cooldown = redis.call("GET", cooldown_key)
if not current_cooldown or current_cooldown == reservation_id then
    redis.call(
        "SET",
        cooldown_key,
        "confirmed:" .. reservation_id,
        "PX",
        cooldown_ttl_ms
    )
end
return had_pending
"""
_PROACTIVE_RENEW_SCRIPT = """
-- proactive_renew
local cooldown_key = KEYS[1]
local slots_key = KEYS[2]
local reservation_id = ARGV[1]
local reservation_ttl_ms = tonumber(ARGV[2])
local slots_ttl_ms = tonumber(ARGV[3])
local pending_member = "pending:" .. reservation_id
local confirmed_member = "confirmed:" .. reservation_id
local redis_time = redis.call("TIME")
local now_ms = tonumber(redis_time[1]) * 1000
    + math.floor(tonumber(redis_time[2]) / 1000)

redis.call("ZREMRANGEBYSCORE", slots_key, "-inf", now_ms)
if redis.call("ZSCORE", slots_key, confirmed_member) then
    return 2
end
if not redis.call("ZSCORE", slots_key, pending_member) then
    return 0
end

redis.call("ZADD", slots_key, now_ms + reservation_ttl_ms, pending_member)
redis.call("PEXPIRE", slots_key, slots_ttl_ms)
if redis.call("GET", cooldown_key) == reservation_id then
    redis.call("PEXPIRE", cooldown_key, reservation_ttl_ms)
end
return 1
"""
_PROACTIVE_RELEASE_SCRIPT = """
-- proactive_release
local cooldown_key = KEYS[1]
local slots_key = KEYS[2]
local reservation_id = ARGV[1]
local pending_member = "pending:" .. reservation_id

local removed = redis.call("ZREM", slots_key, pending_member)
if redis.call("GET", cooldown_key) == reservation_id then
    redis.call("DEL", cooldown_key)
end
return removed
"""
_PROACTIVE_COUNT_SCRIPT = """
-- proactive_count
local slots_key = KEYS[1]
local redis_time = redis.call("TIME")
local now_ms = tonumber(redis_time[1]) * 1000
    + math.floor(tonumber(redis_time[2]) / 1000)
redis.call("ZREMRANGEBYSCORE", slots_key, "-inf", now_ms)
return redis.call("ZCARD", slots_key)
"""
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
redis.call("SET", dedupe_key, "1", "EX", dedupe_ttl_seconds)
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
redis.call("SET", dedupe_key, "1", "EX", dedupe_ttl_seconds)
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
_CHAT_COMMIT_DEDUPE_TTL_SECONDS = 31 * 24 * 60 * 60
_INTERACTION_CLAIM_SCRIPT = """
-- interaction_summary_claim
local pending_key = KEYS[1]
local leases_key = KEYS[2]
local owners_key = KEYS[3]
local owner_token = ARGV[1]
local count = tonumber(ARGV[2])
local lease_ms = tonumber(ARGV[3])
local redis_time = redis.call("TIME")
local now_ms = tonumber(redis_time[1]) * 1000
    + math.floor(tonumber(redis_time[2]) / 1000)

local expired = redis.call("ZRANGEBYSCORE", leases_key, "-inf", now_ms)
for _, user_id in ipairs(expired) do
    redis.call("ZREM", leases_key, user_id)
    redis.call("HDEL", owners_key, user_id)
    redis.call("SADD", pending_key, user_id)
end

local claimed = {}
local candidates = redis.call("SMEMBERS", pending_key)
for _, user_id in ipairs(candidates) do
    if #claimed >= count then
        break
    end
    if not redis.call("ZSCORE", leases_key, user_id) then
        redis.call("SREM", pending_key, user_id)
        redis.call("ZADD", leases_key, now_ms + lease_ms, user_id)
        redis.call("HSET", owners_key, user_id, owner_token)
        table.insert(claimed, user_id)
    end
end
return claimed
"""
_INTERACTION_RENEW_SCRIPT = """
-- interaction_summary_renew
local leases_key = KEYS[1]
local owners_key = KEYS[2]
local user_id = ARGV[1]
local owner_token = ARGV[2]
local lease_ms = tonumber(ARGV[3])

if redis.call("HGET", owners_key, user_id) ~= owner_token then
    return 0
end
if not redis.call("ZSCORE", leases_key, user_id) then
    return 0
end
local redis_time = redis.call("TIME")
local now_ms = tonumber(redis_time[1]) * 1000
    + math.floor(tonumber(redis_time[2]) / 1000)
redis.call("ZADD", leases_key, now_ms + lease_ms, user_id)
return 1
"""
_INTERACTION_SNAPSHOT_SCRIPT = """
-- interaction_summary_snapshot
local source_key = KEYS[1]
local snapshots_key = KEYS[2]
local user_id = ARGV[1]
local processing_key = ARGV[2]
local snapshot_ttl_seconds = tonumber(ARGV[3])

local existing_key = redis.call("HGET", snapshots_key, user_id)
if existing_key then
    if redis.call("EXISTS", existing_key) == 1
       and redis.call("LLEN", existing_key) > 0 then
        return existing_key
    end
    redis.call("HDEL", snapshots_key, user_id)
end

if redis.call("EXISTS", source_key) == 0 then
    return nil
end
if redis.call("LLEN", source_key) == 0 then
    redis.call("DEL", source_key)
    return nil
end
if redis.call("EXISTS", processing_key) ~= 0 then
    return redis.error_reply('processing key already exists')
end
redis.call("RENAME", source_key, processing_key)
redis.call("EXPIRE", processing_key, snapshot_ttl_seconds)
redis.call("HSET", snapshots_key, user_id, processing_key)
return processing_key
"""
_INTERACTION_ACK_SCRIPT = """
-- interaction_summary_ack
local leases_key = KEYS[1]
local owners_key = KEYS[2]
local snapshots_key = KEYS[3]
local user_id = ARGV[1]
local owner_token = ARGV[2]
local processing_key = ARGV[3]

if redis.call("HGET", owners_key, user_id) ~= owner_token then
    return 0
end
local mapped_key = redis.call("HGET", snapshots_key, user_id)
if mapped_key and mapped_key ~= processing_key then
    return 0
end
if mapped_key and mapped_key == processing_key then
    redis.call("DEL", processing_key)
    redis.call("HDEL", snapshots_key, user_id)
end
redis.call("ZREM", leases_key, user_id)
redis.call("HDEL", owners_key, user_id)
return 1
"""
_INTERACTION_REQUEUE_SCRIPT = """
-- interaction_summary_requeue
local leases_key = KEYS[1]
local owners_key = KEYS[2]
local snapshots_key = KEYS[3]
local pending_key = KEYS[4]
local target_key = KEYS[5]
local user_id = ARGV[1]
local owner_token = ARGV[2]
local processing_key = ARGV[3]

if redis.call("HGET", owners_key, user_id) ~= owner_token then
    return 0
end
local mapped_key = redis.call("HGET", snapshots_key, user_id)
if mapped_key and mapped_key == processing_key then
    local old_items = redis.call("LRANGE", processing_key, 0, -1)
    local new_items = redis.call("LRANGE", target_key, 0, -1)
    redis.call("DEL", target_key)
    for _, item in ipairs(old_items) do
        redis.call("RPUSH", target_key, item)
    end
    for _, item in ipairs(new_items) do
        redis.call("RPUSH", target_key, item)
    end
    redis.call("DEL", processing_key)
    redis.call("HDEL", snapshots_key, user_id)
end
redis.call("ZREM", leases_key, user_id)
redis.call("HDEL", owners_key, user_id)
redis.call("SADD", pending_key, user_id)
return 1
"""

_GLOBAL_INTERACTION_SNAPSHOT_TTL_SECONDS = 7 * 24 * 60 * 60


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


def _get_today_4am_timestamp() -> float:
    """获取当前时区今日凌晨 4:00 的时间戳。"""
    now = datetime.now().astimezone()
    today_4am = now.replace(hour=4, minute=0, second=0, microsecond=0)
    return today_4am.timestamp()


def _log_redis_json_error(*, context: str, key: str, raw_item: object) -> None:
    """记录可关联但不包含原始正文的 Redis 反序列化错误。"""
    raw_bytes = (
        raw_item
        if isinstance(raw_item, bytes)
        else str(raw_item).encode("utf-8", errors="replace")
    )
    logger.warning(
        "[KomariMemory] Redis JSON 解析失败: "
        "context={} key={} bytes={} sha256={}",
        context,
        key,
        len(raw_bytes),
        hashlib.sha256(raw_bytes).hexdigest(),
    )


class RedisManager:
    """Redis 操作管理器。"""

    def __init__(self, connection_config: KomariMemoryConfigSchema) -> None:
        """初始化 Redis 管理器。

        Args:
            connection_config: 仅用于建立连接的启动配置快照
        """
        self._connection_config = connection_config
        self._redis: aioredis.Redis | None = None

    @property
    def config(self) -> KomariMemoryConfigSchema:
        """获取当前配置（支持热重载的访问器）。

        Returns:
            当前配置对象
        """
        return get_config()

    async def initialize(self) -> None:
        """初始化 Redis 连接。"""
        db_config = get_shared_redis_config()
        client = aioredis.Redis(
            host=db_config.redis_host,
            port=db_config.redis_port,
            db=self._connection_config.redis_db,
            password=db_config.redis_password or None,
            decode_responses=True,
            encoding="utf-8",
        )
        try:
            await cast("Any", client.ping())
        except Exception:
            await client.aclose()
            raise
        self._redis = client
        logger.info("[KomariMemory] Redis 连接已建立")

    async def close(self) -> None:
        """关闭 Redis 连接。"""
        if self._redis:
            await self._redis.aclose()
            self._redis = None
            logger.info("[KomariMemory] Redis 连接已关闭")

    @property
    def redis(self) -> aioredis.Redis:
        """获取 Redis 客户端实例。"""
        if self._redis is None:
            msg = "Redis 未初始化，请先调用 initialize()"
            raise RuntimeError(msg)
        return self._redis

    async def push_message(
        self,
        group_id: str,
        message: MessageSchema,
    ) -> None:
        """推入消息到缓冲区（连续追加，不再截断）。

        Args:
            group_id: 群组 ID
            message: 消息对象
        """
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
        dedupe_ttl_seconds: int = _CHAT_COMMIT_DEDUPE_TTL_SECONDS,
    ) -> bool:
        """按聊天 operation ID 原子写入一次消息缓冲。"""
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
            max(1, dedupe_ttl_seconds),
        )
        return int(cast("int | str | bytes", result)) == 1

    async def get_buffer(
        self,
        group_id: str,
        limit: int = 100,
    ) -> list[MessageSchema]:
        """获取缓冲区消息。

        Args:
            group_id: 群组 ID
            limit: 最大返回数量

        Returns:
            消息列表
        """
        if limit <= 0:
            return []

        key = RedisKeys.buffer(group_id)
        # 读取尾部最近 N 条消息，Redis 会保持原有顺序返回。
        raw_data = await self.redis.lrange(key, -limit, -1)  # type: ignore[arg-type]

        return [self._deserialize_message(item) for item in raw_data]

    async def claim_conversation_buffer(
        self,
        group_id: str,
        owner_token: str,
        token: str,
    ) -> ConversationSnapshotClaim:
        """认领已有快照，或原子转移普通缓冲并建立 owner lease。"""
        source_key = RedisKeys.buffer(group_id)
        processing_key = RedisKeys.buffer_processing(group_id, token)
        current_key = RedisKeys.buffer_processing_current(group_id)
        lock_key = RedisKeys.buffer_processing_lock(group_id)
        last_message_key = RedisKeys.last_message(group_id)
        session_start_key = RedisKeys.session_start(group_id)
        meta_last_message_key = RedisKeys.buffer_processing_meta_last_message(group_id, token)
        meta_session_start_key = RedisKeys.buffer_processing_meta_session_start(group_id, token)
        result = await self.redis.execute_command(
            "EVAL",
            CONVERSATION_CLAIM_SCRIPT,
            8,
            source_key,
            processing_key,
            current_key,
            lock_key,
            last_message_key,
            session_start_key,
            meta_last_message_key,
            meta_session_start_key,
            owner_token,
            self.config.conversation_processing_lease_seconds * 1000,
            self.config.conversation_snapshot_ttl_seconds,
        )
        return self._parse_conversation_claim(result)

    async def claim_existing_conversation_processing(
        self,
        group_id: str,
        processing_key: str,
        owner_token: str,
    ) -> ConversationSnapshotClaim:
        """为无有效 owner 的现存快照建立租约，兼容旧格式残留。"""
        result = await self.redis.execute_command(
            "EVAL",
            CONVERSATION_CLAIM_EXISTING_SCRIPT,
            3,
            processing_key,
            RedisKeys.buffer_processing_current(group_id),
            RedisKeys.buffer_processing_lock(group_id),
            owner_token,
            self.config.conversation_processing_lease_seconds * 1000,
            self.config.conversation_snapshot_ttl_seconds,
        )
        return self._parse_conversation_claim(result)

    async def get_processing_conversation_buffer(
        self,
        group_id: str,
        processing_key: str,
        owner_token: str,
    ) -> list[MessageSchema]:
        """仅允许当前 owner 读取 processing 快照中的全部消息。"""
        result = await self.redis.execute_command(
            "EVAL",
            CONVERSATION_GET_SCRIPT,
            3,
            processing_key,
            RedisKeys.buffer_processing_current(group_id),
            RedisKeys.buffer_processing_lock(group_id),
            owner_token,
        )
        raw_result = list(cast("list[Any]", result or []))
        if not raw_result or int(raw_result[0]) != 1:
            raise ConversationLeaseLostError(processing_key)
        raw_data = raw_result[1:]
        messages: list[MessageSchema] = []
        for raw_item in raw_data:
            try:
                messages.append(self._deserialize_message(raw_item))
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                _log_redis_json_error(
                    context="conversation_processing",
                    key=processing_key,
                    raw_item=raw_item,
                )
        return messages

    async def renew_processing_conversation_lease(
        self,
        group_id: str,
        processing_key: str,
        owner_token: str,
    ) -> bool:
        """仅由当前 owner 续租 processing 快照。"""
        result = await self.redis.execute_command(
            "EVAL",
            CONVERSATION_RENEW_SCRIPT,
            3,
            processing_key,
            RedisKeys.buffer_processing_current(group_id),
            RedisKeys.buffer_processing_lock(group_id),
            owner_token,
            self.config.conversation_processing_lease_seconds * 1000,
            self.config.conversation_snapshot_ttl_seconds,
        )
        return int(cast("int", result)) == 1

    async def ack_processing_conversation_buffer(
        self,
        group_id: str,
        processing_key: str,
        owner_token: str,
    ) -> bool:
        """仅由当前 owner 确认并删除 processing 快照。"""
        token = self._conversation_processing_token(processing_key)
        result = await self.redis.execute_command(
            "EVAL",
            CONVERSATION_ACK_SCRIPT,
            6,
            processing_key,
            RedisKeys.buffer_processing_current(group_id),
            RedisKeys.buffer_processing_lock(group_id),
            RedisKeys.buffer_processing_meta_last_message(group_id, token),
            RedisKeys.buffer_processing_meta_session_start(group_id, token),
            RedisKeys.buffer_processing_chunks(group_id, token),
            owner_token,
        )
        return int(cast("int", result)) == 1

    async def restore_processing_conversation_buffer(
        self,
        group_id: str,
        processing_key: str,
        owner_token: str,
    ) -> bool:
        """仅由当前 owner 恢复快照，并保持旧记录在新记录之前。"""
        token = self._conversation_processing_token(processing_key)
        result = await self.redis.execute_command(
            "EVAL",
            CONVERSATION_RESTORE_SCRIPT,
            9,
            processing_key,
            RedisKeys.buffer(group_id),
            RedisKeys.buffer_processing_current(group_id),
            RedisKeys.buffer_processing_lock(group_id),
            RedisKeys.last_message(group_id),
            RedisKeys.session_start(group_id),
            RedisKeys.buffer_processing_meta_last_message(group_id, token),
            RedisKeys.buffer_processing_meta_session_start(group_id, token),
            RedisKeys.buffer_processing_chunks(group_id, token),
            owner_token,
        )
        return int(cast("int", result)) >= 0

    async def initialize_conversation_chunk_manifest(
        self,
        *,
        group_id: str,
        processing_key: str,
        owner_token: str,
        manifest_json: str,
    ) -> str:
        """幂等创建分块清单，已有清单必须由调用方比较一致性。"""
        return await self._operate_conversation_chunk_ledger(
            group_id=group_id,
            processing_key=processing_key,
            owner_token=owner_token,
            operation="initialize",
            field="manifest",
            value=manifest_json,
        )

    async def get_conversation_chunk_state(
        self,
        *,
        group_id: str,
        processing_key: str,
        owner_token: str,
        field: str,
    ) -> str | None:
        """读取当前 owner 的单个分块阶段状态。"""
        value = await self._operate_conversation_chunk_ledger(
            group_id=group_id,
            processing_key=processing_key,
            owner_token=owner_token,
            operation="get",
            field=field,
            value="",
        )
        return value or None

    async def set_conversation_chunk_state(
        self,
        *,
        group_id: str,
        processing_key: str,
        owner_token: str,
        field: str,
        value: str,
    ) -> None:
        """写入当前 owner 的单个分块阶段状态。"""
        stored = await self._operate_conversation_chunk_ledger(
            group_id=group_id,
            processing_key=processing_key,
            owner_token=owner_token,
            operation="set",
            field=field,
            value=value,
        )
        if stored != value:
            raise RuntimeError("对话分块阶段状态写入后不一致")

    async def _operate_conversation_chunk_ledger(
        self,
        *,
        group_id: str,
        processing_key: str,
        owner_token: str,
        operation: str,
        field: str,
        value: str,
    ) -> str:
        token = self._conversation_processing_token(processing_key)
        result = await self.redis.execute_command(
            "EVAL",
            CONVERSATION_CHUNK_LEDGER_SCRIPT,
            4,
            processing_key,
            RedisKeys.buffer_processing_current(group_id),
            RedisKeys.buffer_processing_lock(group_id),
            RedisKeys.buffer_processing_chunks(group_id, token),
            owner_token,
            operation,
            field,
            value,
            self.config.conversation_snapshot_ttl_seconds,
        )
        raw_result = list(cast("list[Any]", result or []))
        if not raw_result or int(raw_result[0]) != 1:
            raise ConversationLeaseLostError(processing_key)
        return self._decode_redis_text(raw_result[1]) if len(raw_result) > 1 else ""

    async def dead_letter_processing_conversation_buffer(
        self,
        group_id: str,
        processing_key: str,
        owner_token: str,
        *,
        failure_code: str,
        attempt_count: int,
    ) -> bool:
        """把连续失败的 owner 快照持久移入 dead-letter，保留原始消息。"""
        if self._conversation_processing_group_id(processing_key) != group_id:
            return False
        token = self._conversation_processing_token(processing_key)
        normalized_code = "".join(
            character if character.isalnum() or character in "._-" else "_"
            for character in failure_code
        )[:100]
        result = await self.redis.execute_command(
            "EVAL",
            CONVERSATION_DEAD_LETTER_SCRIPT,
            8,
            processing_key,
            RedisKeys.buffer_processing_current(group_id),
            RedisKeys.buffer_processing_lock(group_id),
            RedisKeys.buffer_processing_meta_last_message(group_id, token),
            RedisKeys.buffer_processing_meta_session_start(group_id, token),
            RedisKeys.buffer_processing_chunks(group_id, token),
            RedisKeys.buffer_processing_dead(group_id, token),
            RedisKeys.BUFFER_PROCESSING_DEAD_INDEX,
            owner_token,
            group_id,
            normalized_code or "UnknownError",
            max(1, attempt_count),
        )
        return int(cast("int | str | bytes", result)) == 1

    async def list_conversation_dead_letters(
        self,
        *,
        limit: int = 100,
    ) -> list[ConversationDeadLetter]:
        """按失败时间倒序返回不含正文的 dead-letter 摘要。"""
        if limit <= 0:
            return []
        raw_keys = await self.redis.zrevrange(
            RedisKeys.BUFFER_PROCESSING_DEAD_INDEX,
            0,
            min(limit, 1000) - 1,
        )
        dead_letters: list[ConversationDeadLetter] = []
        for raw_key in raw_keys:
            processing_key = self._decode_redis_text(raw_key)
            group_id = self._conversation_processing_group_id(processing_key)
            if group_id is None:
                continue
            token = self._conversation_processing_token(processing_key)
            raw_metadata = await cast(
                "Any",
                self.redis.hgetall(
                    RedisKeys.buffer_processing_dead(group_id, token)
                ),
            )
            metadata = {
                self._decode_redis_text(key): self._decode_redis_text(value)
                for key, value in raw_metadata.items()
            }
            if (
                metadata.get("status") != "dead_letter"
                or metadata.get("processing_key") != processing_key
                or not await self.redis.exists(processing_key)
            ):
                continue
            try:
                attempt_count = max(1, int(metadata.get("attempt_count", "1")))
                failed_at_ms = max(0, int(metadata.get("failed_at_ms", "0")))
            except ValueError:
                logger.warning(
                    "[KomariMemory] 跳过元数据损坏的对话 dead-letter: key={}",
                    processing_key,
                )
                continue
            message_count = int(
                cast(
                    "int | str | bytes",
                    await self.redis.execute_command("LLEN", processing_key),
                )
            )
            chunk_state_count = int(
                cast(
                    "int | str | bytes",
                    await self.redis.execute_command(
                        "HLEN",
                        RedisKeys.buffer_processing_chunks(group_id, token),
                    ),
                )
            )
            dead_letters.append(
                ConversationDeadLetter(
                    group_id=group_id,
                    snapshot_id=token,
                    failure_code=metadata.get("failure_code", "UnknownError"),
                    attempt_count=attempt_count,
                    failed_at_ms=failed_at_ms,
                    message_count=max(0, message_count),
                    chunk_state_count=max(0, chunk_state_count),
                )
            )
        return dead_letters

    async def requeue_conversation_dead_letter(
        self,
        *,
        group_id: str,
        snapshot_id: str,
    ) -> int | None:
        """把指定 dead-letter 原子放回活动缓冲区，旧消息保持在前。"""
        if not group_id or ":" in group_id or not snapshot_id or ":" in snapshot_id:
            return None
        processing_key = RedisKeys.buffer_processing(group_id, snapshot_id)
        result = await self.redis.execute_command(
            "EVAL",
            CONVERSATION_DEAD_LETTER_REQUEUE_SCRIPT,
            9,
            processing_key,
            RedisKeys.buffer(group_id),
            RedisKeys.last_message(group_id),
            RedisKeys.session_start(group_id),
            RedisKeys.buffer_processing_meta_last_message(group_id, snapshot_id),
            RedisKeys.buffer_processing_meta_session_start(group_id, snapshot_id),
            RedisKeys.buffer_processing_chunks(group_id, snapshot_id),
            RedisKeys.buffer_processing_dead(group_id, snapshot_id),
            RedisKeys.BUFFER_PROCESSING_DEAD_INDEX,
        )
        restored_count = int(cast("int | str | bytes", result))
        return restored_count if restored_count >= 0 else None

    async def get_orphaned_conversation_processing_keys(self) -> list[tuple[str, str]]:
        """扫描没有有效 owner lease 的对话 processing 快照键。"""
        orphaned: list[tuple[str, str]] = []
        async for raw_key in self.redis.scan_iter(match=RedisKeys.BUFFER_PROCESSING_PATTERN):
            processing_key = self._decode_redis_text(raw_key)
            group_id = self._conversation_processing_group_id(processing_key)
            if group_id is None:
                logger.debug(
                    "[KomariMemory] 跳过非法对话 processing 键: {}",
                    processing_key,
                )
                continue
            if not await self.redis.exists(processing_key):
                continue
            token = self._conversation_processing_token(processing_key)
            if await self.redis.exists(
                RedisKeys.buffer_processing_dead(group_id, token)
            ):
                continue
            current_key = RedisKeys.buffer_processing_current(group_id)
            current_value = await self.redis.get(current_key)
            current_processing = (
                self._decode_redis_text(current_value) if current_value else ""
            )
            lock_key = RedisKeys.buffer_processing_lock(group_id)
            lease_value = await self.redis.get(lock_key)
            lease_processing = self._conversation_lease_processing_key(lease_value)
            if (
                current_processing != processing_key
                or lease_processing != processing_key
            ):
                orphaned.append((group_id, processing_key))
        return orphaned

    async def get_context_around_message(
        self,
        group_id: str,
        message_id: str,
        before: int = 5,
        after: int = 5,
    ) -> list[MessageSchema]:
        """获取指定消息前后的上下文。

        Args:
            group_id: 群组 ID
            message_id: 消息 ID
            before: 获取前面的消息数量
            after: 获取后面的消息数量

        Returns:
            消息列表（按时间排序）
        """
        key = RedisKeys.buffer(group_id)
        # 获取所有缓冲区消息
        raw_data = await self.redis.lrange(key, 0, -1)  # type: ignore[arg-type]

        # 解析所有消息
        all_messages = [self._deserialize_message(msg_item) for msg_item in raw_data]

        # 找到目标消息的索引
        target_index = -1
        for i, msg in enumerate(all_messages):
            if msg.message_id == message_id:
                target_index = i
                break

        # 如果找不到目标消息，返回空列表
        if target_index == -1:
            return []

        # 获取上下文范围
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

    async def should_trigger_summary(
        self,
        group_id: str,
    ) -> bool:
        """判断是否应该触发总结。

        Args:
            group_id: 群组 ID

        Returns:
            是否触发总结
        """
        buffer_key = RedisKeys.buffer(group_id)
        buffer_len = cast("int", await self.redis.execute_command("LLEN", buffer_key))
        should_trigger = False
        config = self.config

        if buffer_len == 0:
            return should_trigger

        if buffer_len >= config.summary_max_buffer_size:
            # 1. 安全上限：防止连续活跃导致缓冲区无限增长。
            logger.debug(
                f"[KomariMemory] 群组 {group_id} buffer 达安全上限: "
                f"{buffer_len}/{config.summary_max_buffer_size}"
            )
            should_trigger = True
        elif (
            (session_start := await self.get_session_start_time(group_id)) is not None
            and session_start < _get_today_4am_timestamp()
        ):
            # 2. 每日 4:00 跨天清理：避免低活跃群多天消息堆积。
            current_tz = datetime.now().astimezone().tzinfo
            logger.debug(
                f"[KomariMemory] 群组 {group_id} 跨天清理: "
                f"buffer={buffer_len} 条（会话自 {datetime.fromtimestamp(session_start, tz=current_tz)}）"
            )
            should_trigger = True
        elif buffer_len >= config.summary_min_messages:
            # 3. 主触发：消息数达标且群聊已空闲足够久。
            last_msg_time = await self.get_last_message_time(group_id)
            if last_msg_time is not None:
                idle_seconds = time.time() - last_msg_time
                if idle_seconds >= config.summary_idle_timeout:
                    logger.debug(
                        f"[KomariMemory] 群组 {group_id} 空闲触发总结: "
                        f"buffer={buffer_len}/{config.summary_min_messages} "
                        f"idle={idle_seconds:.0f}/{config.summary_idle_timeout}s"
                    )
                    should_trigger = True

        return should_trigger

    async def update_last_summary(self, group_id: str) -> None:
        """更新最后总结时间。

        Args:
            group_id: 群组 ID
        """
        key = RedisKeys.last_summary(group_id)
        await self.redis.set(key, time.time())

    async def reserve_proactive_reply(
        self,
        group_id: str,
        reservation_id: str,
        *,
        max_per_hour: int,
        reservation_ttl_seconds: int,
    ) -> ProactiveReservationStatus:
        """原子检查冷却与滑动窗口上限，并预占一个主动回复名额。

        Args:
            group_id: 群组 ID
            reservation_id: 稳定的回复预占 ID
            max_per_hour: 最近一小时允许的最大主动回复数
            reservation_ttl_seconds: 生成与发送阶段的预占有效期
        """
        reservation_ttl_ms = max(1, int(reservation_ttl_seconds * 1000))
        slots_ttl_ms = (
            max(_PROACTIVE_RATE_WINDOW_MS, reservation_ttl_ms)
            + _PROACTIVE_SLOTS_TTL_GRACE_MS
        )
        result = await self.redis.execute_command(
            "EVAL",
            _PROACTIVE_RESERVE_SCRIPT,
            2,
            RedisKeys.proactive_cooldown(group_id),
            RedisKeys.proactive_slots(group_id),
            reservation_id,
            max(1, int(max_per_hour)),
            reservation_ttl_ms,
            slots_ttl_ms,
        )
        match int(cast("int | str | bytes", result)):
            case 0:
                return "reserved"
            case 1:
                return "cooldown"
            case 2:
                return "rate_limited"
            case 3:
                return "duplicate"
            case code:
                msg = f"Redis 返回未知的主动回复预占状态: {code}"
                raise RuntimeError(msg)

    async def confirm_proactive_reply(
        self,
        group_id: str,
        reservation_id: str,
        *,
        cooldown_seconds: int,
    ) -> None:
        """把预占名额原子转换为已送达记录，并开始正式冷却。

        Args:
            group_id: 群组 ID
            reservation_id: 预占 ID
            cooldown_seconds: 回复送达后的冷却秒数
        """
        result = await self.redis.execute_command(
            "EVAL",
            _PROACTIVE_CONFIRM_SCRIPT,
            2,
            RedisKeys.proactive_cooldown(group_id),
            RedisKeys.proactive_slots(group_id),
            reservation_id,
            max(1, int(cooldown_seconds * 1000)),
            _PROACTIVE_RATE_WINDOW_MS + _PROACTIVE_SLOTS_TTL_GRACE_MS,
            _PROACTIVE_RATE_WINDOW_MS,
        )
        code = int(cast("int | str | bytes", result))
        if code == 0:
            logger.warning(
                "[KomariMemory] 主动回复预占已过期，按已送达补记: group={}",
                group_id,
            )
        elif code not in {1, 2}:
            msg = f"Redis 返回未知的主动回复确认状态: {code}"
            raise RuntimeError(msg)

    async def renew_proactive_reply(
        self,
        group_id: str,
        reservation_id: str,
        *,
        reservation_ttl_seconds: int,
    ) -> bool:
        """续期生成中的主动回复预占；已确认记录同样视为成功。"""
        reservation_ttl_ms = max(1, int(reservation_ttl_seconds * 1000))
        slots_ttl_ms = (
            max(_PROACTIVE_RATE_WINDOW_MS, reservation_ttl_ms)
            + _PROACTIVE_SLOTS_TTL_GRACE_MS
        )
        result = await self.redis.execute_command(
            "EVAL",
            _PROACTIVE_RENEW_SCRIPT,
            2,
            RedisKeys.proactive_cooldown(group_id),
            RedisKeys.proactive_slots(group_id),
            reservation_id,
            reservation_ttl_ms,
            slots_ttl_ms,
        )
        code = int(cast("int | str | bytes", result))
        if code not in {0, 1, 2}:
            msg = f"Redis 返回未知的主动回复续期状态: {code}"
            raise RuntimeError(msg)
        return code in {1, 2}

    async def release_proactive_reply(
        self,
        group_id: str,
        reservation_id: str,
    ) -> bool:
        """释放尚未确认送达的主动回复预占；重复释放安全。

        Args:
            group_id: 群组 ID
            reservation_id: 预占 ID

        Returns:
            是否移除了待确认名额
        """
        result = await self.redis.execute_command(
            "EVAL",
            _PROACTIVE_RELEASE_SCRIPT,
            2,
            RedisKeys.proactive_cooldown(group_id),
            RedisKeys.proactive_slots(group_id),
            reservation_id,
        )
        return int(cast("int | str | bytes", result)) > 0

    async def get_proactive_count(self, group_id: str) -> int:
        """获取最近一小时已送达与正在生成的主动回复名额数。

        Args:
            group_id: 群组 ID

        Returns:
            当前计数值
        """
        result = await self.redis.execute_command(
            "EVAL",
            _PROACTIVE_COUNT_SCRIPT,
            1,
            RedisKeys.proactive_slots(group_id),
        )
        return int(cast("int | str | bytes", result))

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
        dedupe_ttl_seconds: int = _CHAT_COMMIT_DEDUPE_TTL_SECONDS,
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
            max(1, dedupe_ttl_seconds),
        )
        return int(cast("int | str | bytes", result)) == 1

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

    async def add_pending_interaction_summary(self, user_id: str) -> None:
        """加入跨群互动事件待总结集合。"""
        await self.redis.execute_command(
            "SADD",
            RedisKeys.GLOBAL_INTERACTION_PENDING,
            user_id,
        )

    async def claim_pending_interaction_summaries(
        self,
        *,
        owner_token: str,
        count: int = 100,
        lease_seconds: int = 1800,
    ) -> list[str]:
        """原子认领待总结用户，并回收超过可见性超时的旧租约。"""
        if count <= 0:
            return []
        values = await self.redis.execute_command(
            "EVAL",
            _INTERACTION_CLAIM_SCRIPT,
            3,
            RedisKeys.GLOBAL_INTERACTION_PENDING,
            RedisKeys.GLOBAL_INTERACTION_LEASES,
            RedisKeys.GLOBAL_INTERACTION_LEASE_OWNERS,
            owner_token,
            count,
            max(1, lease_seconds) * 1000,
        )
        if values is None:
            return []
        if isinstance(values, (str, bytes)):
            values = [values]
        return [self._decode_redis_text(value) for value in values if value]

    async def snapshot_global_interactions(self, user_id: str, token: str) -> str | None:
        """复用尚存的 processing 快照，或原子转移当前互动缓冲。"""
        source_key = RedisKeys.global_interaction(user_id)
        processing_key = RedisKeys.global_interaction_processing(user_id, token)
        result = await self.redis.execute_command(
            "EVAL",
            _INTERACTION_SNAPSHOT_SCRIPT,
            2,
            source_key,
            RedisKeys.GLOBAL_INTERACTION_SNAPSHOTS,
            user_id,
            processing_key,
            _GLOBAL_INTERACTION_SNAPSHOT_TTL_SECONDS,
        )
        return self._decode_redis_text(result) if result else None

    async def renew_interaction_summary_lease(
        self,
        *,
        user_id: str,
        owner_token: str,
        lease_seconds: int,
    ) -> bool:
        """仅由当前 owner 续期单个互动总结租约。"""
        result = await self.redis.execute_command(
            "EVAL",
            _INTERACTION_RENEW_SCRIPT,
            2,
            RedisKeys.GLOBAL_INTERACTION_LEASES,
            RedisKeys.GLOBAL_INTERACTION_LEASE_OWNERS,
            user_id,
            owner_token,
            max(1, lease_seconds) * 1000,
        )
        return int(cast("int | str | bytes", result)) == 1

    async def get_processing_global_interactions(
        self,
        processing_key: str,
    ) -> list[dict[str, Any]]:
        """读取 processing 快照中的全部互动记录。"""
        raw_items = await self.redis.lrange(processing_key, 0, -1)  # type: ignore[arg-type]
        records: list[dict[str, Any]] = []
        for raw_item in raw_items:
            try:
                parsed = json.loads(raw_item)
            except (TypeError, ValueError):
                _log_redis_json_error(
                    context="global_interaction_processing",
                    key=processing_key,
                    raw_item=raw_item,
                )
                continue
            if isinstance(parsed, dict):
                records.append(parsed)
        return records

    async def ack_processing_global_interactions(
        self,
        *,
        user_id: str,
        owner_token: str,
        processing_key: str,
    ) -> bool:
        """仅由当前租约 owner 删除快照并确认成功。"""
        result = await self.redis.execute_command(
            "EVAL",
            _INTERACTION_ACK_SCRIPT,
            3,
            RedisKeys.GLOBAL_INTERACTION_LEASES,
            RedisKeys.GLOBAL_INTERACTION_LEASE_OWNERS,
            RedisKeys.GLOBAL_INTERACTION_SNAPSHOTS,
            user_id,
            owner_token,
            processing_key,
        )
        return int(cast("int | str | bytes", result)) == 1

    async def requeue_processing_global_interactions(
        self,
        *,
        user_id: str,
        owner_token: str,
        processing_key: str,
    ) -> bool:
        """仅由当前 owner 恢复快照、释放租约并立即重新入队。"""
        target_key = RedisKeys.global_interaction(user_id)
        result = await self.redis.execute_command(
            "EVAL",
            _INTERACTION_REQUEUE_SCRIPT,
            5,
            RedisKeys.GLOBAL_INTERACTION_LEASES,
            RedisKeys.GLOBAL_INTERACTION_LEASE_OWNERS,
            RedisKeys.GLOBAL_INTERACTION_SNAPSHOTS,
            RedisKeys.GLOBAL_INTERACTION_PENDING,
            target_key,
            user_id,
            owner_token,
            processing_key,
        )
        return int(cast("int | str | bytes", result)) == 1

    async def get_users_with_global_interaction_buffer(self) -> list[str]:
        """扫描所有存在跨群互动缓冲的用户 ID。"""
        users: list[str] = []
        excluded = {
            RedisKeys.GLOBAL_INTERACTION_PENDING,
            RedisKeys.GLOBAL_INTERACTION_LEASES,
            RedisKeys.GLOBAL_INTERACTION_LEASE_OWNERS,
            RedisKeys.GLOBAL_INTERACTION_SNAPSHOTS,
        }
        processing_prefix = f"{RedisKeys.PREFIX}:global_interaction:processing:"
        async for key in self.redis.scan_iter(match=RedisKeys.GLOBAL_INTERACTION_PATTERN):
            key_text = self._decode_redis_text(key)
            if key_text in excluded or key_text.startswith(processing_prefix):
                continue
            user_id = key_text.rsplit(":", 1)[-1]
            buffer_len = cast("int", await self.redis.execute_command("LLEN", key_text))
            if user_id and buffer_len > 0:
                users.append(user_id)
        return users

    async def delete_buffer(
        self,
        group_id: str,
    ) -> None:
        """清空消息缓冲区及相关元数据。

        Args:
            group_id: 群组 ID
        """
        pipe = self.redis.pipeline()
        pipe.delete(RedisKeys.buffer(group_id))
        pipe.delete(RedisKeys.last_message(group_id))
        pipe.delete(RedisKeys.session_start(group_id))
        await pipe.execute()

    async def get_active_groups(self) -> list[str]:
        """获取有活跃消息缓冲的群组列表。

        Returns:
            群组 ID 列表
        """
        pattern = RedisKeys.BUFFER_PATTERN
        keys = []
        excluded_keys = {RedisKeys.BUFFER_PROCESSING_DEAD_INDEX}
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

    @classmethod
    def _parse_conversation_claim(cls, value: object) -> ConversationSnapshotClaim:
        raw = list(cast("list[Any]", value or []))
        if not raw:
            return ConversationSnapshotClaim(status="empty")
        status_code = int(raw[0])
        processing_key = cls._decode_redis_text(raw[1]) if len(raw) > 1 and raw[1] else None
        match status_code:
            case 1:
                return ConversationSnapshotClaim(
                    status="claimed",
                    processing_key=processing_key,
                )
            case 2:
                return ConversationSnapshotClaim(
                    status="busy",
                    processing_key=processing_key,
                )
            case _:
                return ConversationSnapshotClaim(status="empty")

    @classmethod
    def _conversation_lease_processing_key(cls, value: object | None) -> str:
        if value is None:
            return ""
        raw = cls._decode_redis_text(value)
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return raw
        if not isinstance(payload, dict):
            return ""
        return str(payload.get("processing_key", ""))

    @staticmethod
    def _conversation_processing_token(processing_key: str) -> str:
        return processing_key.rsplit(":", 1)[-1]

    @staticmethod
    def _conversation_processing_group_id(processing_key: str) -> str | None:
        prefix = f"{RedisKeys.PREFIX}:buffer:processing:"
        if not processing_key.startswith(prefix):
            return None
        remainder = processing_key.removeprefix(prefix)
        group_id, separator, token = remainder.partition(":")
        if not group_id or not separator or not token or ":" in token:
            return None
        return group_id
