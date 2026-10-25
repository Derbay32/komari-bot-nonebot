"""Komari Memory Redis 操作管理器。"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from nonebot import logger

from komari_bot.context.redis_store import RecentContextStore, _log_redis_json_error

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
    ConversationChunkStateMismatchError,
    ConversationDeadLetter,
    ConversationLeaseLostError,
    ConversationSnapshotClaim,
)
from .redis_keys import RedisKeys

if TYPE_CHECKING:
    import redis.asyncio as aioredis

    from komari_bot.context.redis_store import MessageSchema

    from ..config_schema import KomariMemoryConfigSchema

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

#: 对话 processing 快照的休眠安全 TTIL 下限（秒）：受限群休眠窗口内不得因
#: 临时快照 TTL 清零而丢失安全进度（ADR-0012 "若现有 TTL 会让安全进度在休眠
#: 中消失，实现必须调整以保留进度"）。实际生效 TTL = max(配置值, 本下限)，
#: 恢复后仍复用剩余 TTL，不重新满额（补项2）。
_PROCESSING_SNAPSHOT_DORMANCY_TTL_SECONDS = 7 * 24 * 60 * 60


def _get_today_4am_timestamp() -> float:
    """获取当前时区今日凌晨 4:00 的时间戳。"""
    now = datetime.now().astimezone()
    today_4am = now.replace(hour=4, minute=0, second=0, microsecond=0)
    return today_4am.timestamp()


class RedisManager(RecentContextStore):
    """Memory-specific Redis specialization over the shared supplied client."""

    def __init__(self, redis: aioredis.Redis) -> None:
        """Use the client owned by komari_context; never create or close it."""
        super().__init__(redis)

    @property
    def config(self) -> KomariMemoryConfigSchema:
        """获取当前配置（支持热重载的访问器）。"""
        return get_config()

    def _processing_snapshot_ttl(self) -> int:
        """生效的对话 processing 快照 TTL：不低于休眠安全下限（冻结跨休眠窗）。"""
        return max(
            int(self.config.conversation_snapshot_ttl_seconds),
            _PROCESSING_SNAPSHOT_DORMANCY_TTL_SECONDS,
        )

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
            self._processing_snapshot_ttl(),
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
            self._processing_snapshot_ttl(),
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
            self._processing_snapshot_ttl(),
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
            raise ConversationChunkStateMismatchError(
                field=field, expected=value, stored=stored
            )

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
            self._processing_snapshot_ttl(),
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
