"""Komari Memory Redis 操作管理器。"""

import json
import time
from dataclasses import dataclass

import redis.asyncio as aioredis
from nonebot import logger

from ..config_schema import KomariMemoryConfigSchema


@dataclass(frozen=True)
class MessageSchema:
    """消息数据结构。"""

    user_id: str
    group_id: str
    content: str
    timestamp: float
    message_id: str


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
        from .. import get_config

        return get_config()

    async def initialize(self) -> None:
        """初始化 Redis 连接。"""
        # 构建连接 URL
        password_part = (
            f":{self.config.redis_password}@" if self.config.redis_password else ""
        )
        redis_url = (
            f"redis://{password_part}{self.config.redis_host}:"
            f"{self.config.redis_port}/{self.config.redis_db}"
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
        """推入消息到缓冲区，并执行 LTRIM。

        Args:
            group_id: 群组 ID
            message: 消息对象
        """
        key = f"komari_memory:buffer:{group_id}"
        data = {
            "user_id": message.user_id,
            "group_id": message.group_id,
            "content": message.content,
            "timestamp": message.timestamp,
            "message_id": message.message_id,
        }

        pipe = self.redis.pipeline()
        pipe.rpush(key, json.dumps(data))
        pipe.ltrim(key, 0, self.config.message_buffer_size - 1)
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
        key = f"komari_memory:buffer:{group_id}"
        raw_data = await self.redis.lrange(key, 0, limit - 1)  # type: ignore[arg-type]

        messages: list[MessageSchema] = []
        for item in raw_data:
            data = json.loads(item)
            messages.append(
                MessageSchema(
                    user_id=data["user_id"],
                    group_id=data["group_id"],
                    content=data["content"],
                    timestamp=data["timestamp"],
                    message_id=data["message_id"],
                )
            )

        return messages

    async def increment_tokens(
        self,
        group_id: str,
        count: int,
    ) -> int:
        """增加 Token 计数。

        Args:
            group_id: 群组 ID
            count: 增加的数量

        Returns:
            当前计数值
        """
        key = f"komari_memory:tokens:{group_id}"
        return await self.redis.incrby(key, count)

    async def get_tokens(self, group_id: str) -> int:
        """获取当前 Token 计数。

        Args:
            group_id: 群组 ID

        Returns:
            当前计数值
        """
        key = f"komari_memory:tokens:{group_id}"
        value = await self.redis.get(key)
        return int(value) if value else 0

    async def reset_tokens(self, group_id: str) -> None:
        """重置 Token 计数。

        Args:
            group_id: 群组 ID
        """
        key = f"komari_memory:tokens:{group_id}"
        await self.redis.delete(key)

    async def increment_message_count(
        self,
        group_id: str,
    ) -> int:
        """增加消息计数。

        Args:
            group_id: 群组 ID

        Returns:
            当前计数值
        """
        key = f"komari_memory:messages:{group_id}"
        return await self.redis.incrby(key, 1)

    async def get_message_count(self, group_id: str) -> int:
        """获取当前消息计数。

        Args:
            group_id: 群组 ID

        Returns:
            当前计数值
        """
        key = f"komari_memory:messages:{group_id}"
        value = await self.redis.get(key)
        return int(value) if value else 0

    async def reset_message_count(self, group_id: str) -> None:
        """重置消息计数。

        Args:
            group_id: 群组 ID
        """
        key = f"komari_memory:messages:{group_id}"
        await self.redis.delete(key)

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
        # 1. 检查消息数量阈值（优先级最高）
        message_count = await self.get_message_count(group_id)
        if message_count >= self.config.summary_message_threshold:
            logger.debug(
                f"[KomariMemory] 群组 {group_id} 消息数达标: "
                f"{message_count}/{self.config.summary_message_threshold}"
            )
            return True

        # 2. 检查时间阈值（优先级次之）
        last_key = f"komari_memory:last_summary:{group_id}"
        last_summary = await self.redis.get(last_key)
        if last_summary:
            elapsed = time.time() - float(last_summary)
            if elapsed >= self.config.summary_time_threshold:
                logger.debug(
                    f"[KomariMemory] 群组 {group_id} 时间达标: "
                    f"{elapsed:.0f}/{self.config.summary_time_threshold} 秒"
                )
                return True

        # 3. 检查 Token 阈值（备用触发条件）
        token_count = await self.get_tokens(group_id)
        if token_count >= self.config.summary_token_threshold:
            logger.debug(
                f"[KomariMemory] 群组 {group_id} Token 数达标: "
                f"{token_count}/{self.config.summary_token_threshold}"
            )
            return True

        return False

    async def update_last_summary(self, group_id: str) -> None:
        """更新最后总结时间。

        Args:
            group_id: 群组 ID
        """
        key = f"komari_memory:last_summary:{group_id}"
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
        key = f"komari_memory:proactive:cd:{group_id}"
        await self.redis.set(key, "1", ex=seconds)

    async def is_on_cooldown(self, group_id: str) -> bool:
        """检查是否在冷却中。

        Args:
            group_id: 群组 ID

        Returns:
            是否在冷却中
        """
        key = f"komari_memory:proactive:cd:{group_id}"
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
        key = f"komari_memory:proactive:count:{group_id}:{current_hour}"

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
        key = f"komari_memory:proactive:count:{group_id}:{current_hour}"
        value = await self.redis.get(key)
        return int(value) if value else 0

    async def delete_buffer(
        self,
        group_id: str,
    ) -> None:
        """清空消息缓冲区。

        Args:
            group_id: 群组 ID
        """
        key = f"komari_memory:buffer:{group_id}"
        await self.redis.delete(key)

    async def get_active_groups(self) -> list[str]:
        """获取有活跃消息缓冲的群组列表。

        Returns:
            群组 ID 列表
        """
        pattern = "komari_memory:buffer:*"
        keys = []
        async for key in self.redis.scan_iter(match=pattern):
            # 提取 group_id
            group_id = (
                key.decode().split(":")[-1]
                if isinstance(key, bytes)
                else key.split(":")[-1]
            )
            keys.append(group_id)
        return keys
