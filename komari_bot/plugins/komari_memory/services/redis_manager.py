"""Komari Memory Redis 操作管理器。"""

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

import redis.asyncio as aioredis
from nonebot import logger

from komari_bot.common.database_config import get_shared_database_config

from ..config_schema import KomariMemoryConfigSchema
from .config_interface import get_config
from .redis_keys import RedisKeys


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


class RedisManager:
    """Redis 操作管理器。"""

    def __init__(self, config: KomariMemoryConfigSchema) -> None:
        """初始化 Redis 管理器。

        Args:
            config: 插件配置
        """
        self._config = config
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
        db_config = get_shared_database_config()

        # 构建连接 URL
        password_part = (
            f":{db_config.redis_password}@" if db_config.redis_password else ""
        )
        redis_url = (
            f"redis://{password_part}{db_config.redis_host}:"
            f"{db_config.redis_port}/{self.config.redis_db}"
        )

        self._redis = await aioredis.from_url(
            redis_url, decode_responses=True, encoding="utf-8"
        )
        logger.info("[KomariMemory] Redis 连接已建立")

    async def close(self) -> None:
        """关闭 Redis 连接。"""
        if self._redis:
            await self._redis.close()
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

    async def snapshot_conversation_buffer(
        self,
        group_id: str,
        token: str,
    ) -> str | None:
        """原子转移群聊消息缓冲到 processing 快照键。"""
        source_key = RedisKeys.buffer(group_id)
        processing_key = RedisKeys.buffer_processing(group_id, token)
        lock_key = RedisKeys.buffer_processing_lock(group_id)
        last_message_key = RedisKeys.last_message(group_id)
        session_start_key = RedisKeys.session_start(group_id)
        meta_last_message_key = RedisKeys.buffer_processing_meta_last_message(group_id, token)
        meta_session_start_key = RedisKeys.buffer_processing_meta_session_start(group_id, token)
        script = """
        local locked_key = redis.call('GET', KEYS[3])
        if locked_key and locked_key ~= '' then
            if redis.call('EXISTS', locked_key) ~= 0 then
                return locked_key
            end
            redis.call('DEL', KEYS[3])
        end
        if redis.call('EXISTS', KEYS[1]) == 0 then
            return nil
        end
        if redis.call('LLEN', KEYS[1]) == 0 then
            redis.call('DEL', KEYS[1])
            return nil
        end
        if redis.call('EXISTS', KEYS[2]) ~= 0 then
            return redis.error_reply('conversation processing key already exists')
        end
        redis.call('RENAME', KEYS[1], KEYS[2])
        local last_message = redis.call('GET', KEYS[4])
        if last_message then
            redis.call('SET', KEYS[6], last_message)
        end
        local session_start = redis.call('GET', KEYS[5])
        if session_start then
            redis.call('SET', KEYS[7], session_start)
        end
        redis.call('DEL', KEYS[4], KEYS[5])
        redis.call('SET', KEYS[3], KEYS[2])
        for index = 2, 7 do
            redis.call('EXPIRE', KEYS[index], tonumber(ARGV[1]))
        end
        return KEYS[2]
        """
        result = await self.redis.execute_command(
            "EVAL",
            script,
            7,
            source_key,
            processing_key,
            lock_key,
            last_message_key,
            session_start_key,
            meta_last_message_key,
            meta_session_start_key,
            self.config.conversation_snapshot_ttl_seconds,
        )
        return self._decode_redis_text(result) if result else None

    async def get_processing_conversation_buffer(
        self,
        processing_key: str,
    ) -> list[MessageSchema]:
        """读取 processing 快照中的全部群聊消息。"""
        raw_data = await self.redis.lrange(processing_key, 0, -1)  # type: ignore[arg-type]
        messages: list[MessageSchema] = []
        for raw_item in raw_data:
            try:
                messages.append(self._deserialize_message(raw_item))
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                logger.warning("[KomariMemory] 对话 processing 消息 JSON 解析失败: {}", raw_item)
        return messages

    async def delete_processing_conversation_buffer(
        self,
        group_id: str,
        processing_key: str,
    ) -> None:
        """删除对话 processing 快照及其元数据，不影响新普通缓冲。"""
        token = self._conversation_processing_token(processing_key)
        lock_key = RedisKeys.buffer_processing_lock(group_id)
        meta_last_message_key = RedisKeys.buffer_processing_meta_last_message(group_id, token)
        meta_session_start_key = RedisKeys.buffer_processing_meta_session_start(group_id, token)
        script = """
        redis.call('DEL', KEYS[1], KEYS[3], KEYS[4])
        if redis.call('GET', KEYS[2]) == KEYS[1] then
            redis.call('DEL', KEYS[2])
        end
        return 1
        """
        await self.redis.execute_command(
            "EVAL",
            script,
            4,
            processing_key,
            lock_key,
            meta_last_message_key,
            meta_session_start_key,
        )

    async def restore_processing_conversation_buffer(
        self,
        group_id: str,
        processing_key: str,
    ) -> None:
        """将对话 processing 快照恢复回普通缓冲，并保持旧记录在新记录之前。"""
        token = self._conversation_processing_token(processing_key)
        target_key = RedisKeys.buffer(group_id)
        lock_key = RedisKeys.buffer_processing_lock(group_id)
        last_message_key = RedisKeys.last_message(group_id)
        session_start_key = RedisKeys.session_start(group_id)
        meta_last_message_key = RedisKeys.buffer_processing_meta_last_message(group_id, token)
        meta_session_start_key = RedisKeys.buffer_processing_meta_session_start(group_id, token)
        script = """
        local old_items = redis.call('LRANGE', KEYS[1], 0, -1)
        local new_items = redis.call('LRANGE', KEYS[2], 0, -1)
        redis.call('DEL', KEYS[2])
        for _, item in ipairs(old_items) do
            redis.call('RPUSH', KEYS[2], item)
        end
        for _, item in ipairs(new_items) do
            redis.call('RPUSH', KEYS[2], item)
        end
        redis.call('DEL', KEYS[1])
        if redis.call('GET', KEYS[3]) == KEYS[1] then
            redis.call('DEL', KEYS[3])
        end
        local session_start = redis.call('GET', KEYS[7])
        if session_start then
            redis.call('SET', KEYS[5], session_start)
        elseif #old_items > 0 or #new_items > 0 then
            local first_item = old_items[1] or new_items[1]
            local timestamp = string.match(first_item, '"timestamp"%s*:%s*([0-9%.]+)')
            if timestamp then
                redis.call('SET', KEYS[5], timestamp)
            end
        end
        local last_item = new_items[#new_items] or old_items[#old_items]
        if last_item then
            local timestamp = string.match(last_item, '"timestamp"%s*:%s*([0-9%.]+)')
            if timestamp then
                redis.call('SET', KEYS[4], timestamp)
            end
        else
            local last_message = redis.call('GET', KEYS[6])
            if last_message then
                redis.call('SET', KEYS[4], last_message)
            end
        end
        redis.call('DEL', KEYS[6], KEYS[7])
        return #old_items
        """
        await self.redis.execute_command(
            "EVAL",
            script,
            7,
            processing_key,
            target_key,
            lock_key,
            last_message_key,
            session_start_key,
            meta_last_message_key,
            meta_session_start_key,
        )

    async def get_orphaned_conversation_processing_keys(self) -> list[tuple[str, str]]:
        """扫描无有效处理锁的对话 processing 快照键。"""
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
            lock_key = RedisKeys.buffer_processing_lock(group_id)
            locked_key = await self.redis.get(lock_key)
            locked_key_text = self._decode_redis_text(locked_key) if locked_key else ""
            if locked_key_text != processing_key:
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

        if buffer_len == 0:
            return should_trigger

        if buffer_len >= self.config.summary_max_buffer_size:
            # 1. 安全上限：防止连续活跃导致缓冲区无限增长。
            logger.debug(
                f"[KomariMemory] 群组 {group_id} buffer 达安全上限: "
                f"{buffer_len}/{self.config.summary_max_buffer_size}"
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
        elif buffer_len >= self.config.summary_min_messages:
            # 3. 主触发：消息数达标且群聊已空闲足够久。
            last_msg_time = await self.get_last_message_time(group_id)
            if last_msg_time is not None:
                idle_seconds = time.time() - last_msg_time
                if idle_seconds >= self.config.summary_idle_timeout:
                    logger.debug(
                        f"[KomariMemory] 群组 {group_id} 空闲触发总结: "
                        f"buffer={buffer_len}/{self.config.summary_min_messages} "
                        f"idle={idle_seconds:.0f}/{self.config.summary_idle_timeout}s"
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

    async def set_cooldown(
        self,
        group_id: str,
        seconds: int,
    ) -> None:
        """设置主动回复冷却。

        Args:
            group_id: 群组 ID
            seconds: 冷却时间（秒）
        """
        key = RedisKeys.proactive_cooldown(group_id)
        await self.redis.set(key, "1", ex=seconds)

    async def is_on_cooldown(self, group_id: str) -> bool:
        """检查是否在冷却中。

        Args:
            group_id: 群组 ID

        Returns:
            是否在冷却中
        """
        key = RedisKeys.proactive_cooldown(group_id)
        return await self.redis.exists(key) > 0

    async def increment_proactive_count(
        self,
        group_id: str,
    ) -> int:
        """增加当前小时的主动回复计数。

        Args:
            group_id: 群组 ID

        Returns:
            当前计数值
        """
        current_hour = int(time.time() // 3600)
        key = RedisKeys.proactive_count(group_id, current_hour)

        pipe = self.redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, 3600)
        results = await pipe.execute()

        return results[0]

    async def get_proactive_count(self, group_id: str) -> int:
        """获取当前小时的主动回复计数。

        Args:
            group_id: 群组 ID

        Returns:
            当前计数值
        """
        current_hour = int(time.time() // 3600)
        key = RedisKeys.proactive_count(group_id, current_hour)
        value = await self.redis.get(key)
        return int(value) if value else 0

    async def is_favor_greeted(self, group_id: str, user_id: str) -> bool:
        """检查用户当天是否已追加过好感文案。"""
        key = RedisKeys.favor_greeted(group_id, user_id)
        return await self.redis.exists(key) > 0

    async def mark_favor_greeted(self, group_id: str, user_id: str) -> None:
        """标记用户当天已追加过好感文案，并设置到当天结束的 TTL。"""
        key = RedisKeys.favor_greeted(group_id, user_id)
        now = datetime.now().astimezone()
        end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0)
        if end_of_day <= now:
            end_of_day += timedelta(days=1)
        ttl = max(1, int((end_of_day - now).total_seconds()) + 1)
        await self.redis.set(key, "1", ex=ttl)

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
        pipe = self.redis.pipeline()
        pipe.rpush(key, *payloads)
        pipe.llen(key)
        results = await pipe.execute()
        buffer_len = int(results[-1] or 0)
        if buffer_len >= trigger_size:
            await self.add_pending_interaction_summary(user_id)

    async def add_pending_interaction_summary(self, user_id: str) -> None:
        """加入跨群互动事件待总结集合。"""
        await self.redis.execute_command(
            "SADD",
            RedisKeys.GLOBAL_INTERACTION_PENDING,
            user_id,
        )

    async def pop_pending_interaction_summaries(self, count: int = 100) -> list[str]:
        """批量弹出待总结用户 ID。"""
        if count <= 0:
            return []
        values = await self.redis.execute_command(
            "SPOP",
            RedisKeys.GLOBAL_INTERACTION_PENDING,
            count,
        )
        if values is None:
            return []
        if isinstance(values, (str, bytes)):
            values = [values]
        return [self._decode_redis_text(value) for value in values if value]

    async def snapshot_global_interactions(self, user_id: str, token: str) -> str | None:
        """原子转移用户互动缓冲到 processing 快照键。"""
        source_key = RedisKeys.global_interaction(user_id)
        processing_key = RedisKeys.global_interaction_processing(user_id, token)
        script = """
        if redis.call('EXISTS', KEYS[1]) == 0 then
            return nil
        end
        if redis.call('LLEN', KEYS[1]) == 0 then
            redis.call('DEL', KEYS[1])
            return nil
        end
        if redis.call('EXISTS', KEYS[2]) ~= 0 then
            return redis.error_reply('processing key already exists')
        end
        redis.call('RENAME', KEYS[1], KEYS[2])
        redis.call('EXPIRE', KEYS[2], tonumber(ARGV[1]))
        return KEYS[2]
        """
        result = await self.redis.execute_command(
            "EVAL",
            script,
            2,
            source_key,
            processing_key,
            86400,
        )
        return self._decode_redis_text(result) if result else None

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
                logger.warning("[KomariMemory] 跨群互动缓冲 JSON 解析失败: {}", raw_item)
                continue
            if isinstance(parsed, dict):
                records.append(parsed)
        return records

    async def delete_processing_global_interactions(self, processing_key: str) -> None:
        """删除 processing 快照键。"""
        await self.redis.delete(processing_key)

    async def restore_processing_global_interactions(
        self,
        user_id: str,
        processing_key: str,
    ) -> None:
        """将 processing 快照恢复回原缓冲，并保持旧记录在新记录之前。"""
        target_key = RedisKeys.global_interaction(user_id)
        script = """
        local old_items = redis.call('LRANGE', KEYS[1], 0, -1)
        if #old_items == 0 then
            redis.call('DEL', KEYS[1])
            return 0
        end
        local new_items = redis.call('LRANGE', KEYS[2], 0, -1)
        redis.call('DEL', KEYS[2])
        redis.call('RPUSH', KEYS[2], unpack(old_items))
        if #new_items > 0 then
            redis.call('RPUSH', KEYS[2], unpack(new_items))
        end
        redis.call('DEL', KEYS[1])
        return #old_items
        """
        await self.redis.execute_command("EVAL", script, 2, processing_key, target_key)

    async def get_users_with_global_interaction_buffer(self) -> list[str]:
        """扫描所有存在跨群互动缓冲的用户 ID。"""
        users: list[str] = []
        excluded = {RedisKeys.GLOBAL_INTERACTION_PENDING}
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
        excluded_prefixes = (
            f"{RedisKeys.PREFIX}:buffer:processing:",
            f"{RedisKeys.PREFIX}:buffer:processing_lock:",
            f"{RedisKeys.PREFIX}:buffer:processing_meta:",
        )
        async for key in self.redis.scan_iter(match=pattern):
            key_text = self._decode_redis_text(key)
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
