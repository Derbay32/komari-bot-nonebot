"""Komari Memory 消息处理核心。"""

import time

from nonebot import logger
from nonebot.adapters.onebot.v11 import GroupMessageEvent

from .. import get_config
from ..services.bert_client import score_message
from ..services.llm_service import generate_reply
from ..services.memory_service import MemoryService
from ..services.prompt_builder import build_prompt
from ..services.redis_manager import MessageSchema, RedisManager


class MessageHandler:
    """消息处理核心。"""

    def __init__(
        self,
        redis: RedisManager,
        memory: MemoryService,
    ) -> None:
        """初始化消息处理器。

        Args:
            redis: Redis 管理器
            memory: 记忆服务
        """
        self.redis = redis
        self.memory = memory

    async def process_message(
        self,
        event: GroupMessageEvent,
        context_message: str | None = None,
    ) -> str | None:
        """处理群聊消息的主流程。

        Args:
            event: 群聊消息事件
            context_message: 上一句消息

        Returns:
            回复内容，如果不需要回复则返回 None
        """
        # 获取最新配置
        config = get_config()

        # 提取消息信息
        user_id = str(event.user_id)
        group_id = str(event.group_id)
        message_content = event.get_plaintext()
        message_id = str(event.message_id)

        message = MessageSchema(
            user_id=user_id,
            group_id=group_id,
            content=message_content,
            timestamp=time.time(),
            message_id=message_id,
        )

        # 调用 BERT 服务评分
        score = await score_message(
            message=message_content,
            context=context_message or "",
            user_id=user_id,
            group_id=group_id,
            config=config,
        )

        logger.debug(
            f"[KomariMemory] 消息评分: {score:.2f} (group={group_id}, user={user_id})"
        )

        # 根据评分分类处理
        if score < 0.3:  # 低价值
            await self._handle_low_value(message)
            return None

        if score >= config.proactive_score_threshold:  # 主动回复阈值
            return await self._handle_interrupt_signal(message, score)

        # 普通消息
        await self._handle_normal_message(message)
        return None

    async def _handle_low_value(self, message: MessageSchema) -> None:
        """处理低价值消息。

        Args:
            message: 消息对象
        """
        # 存入 Redis 但不计入 Token
        await self.redis.push_message(message.group_id, message)
        logger.debug(f"[KomariMemory] 低价值消息已过滤: {message.content[:30]}...")

    async def _handle_normal_message(self, message: MessageSchema) -> None:
        """处理普通消息。

        Args:
            message: 消息对象
        """
        # 存入 Redis 并计入 Token
        await self.redis.push_message(message.group_id, message)

        # 估算 Token (中文约 1.5 字 = 1 token，这里简化处理)
        token_count = len(message.content)
        await self.redis.increment_tokens(message.group_id, token_count)

        logger.debug(
            f"[KomariMemory] 消息已存储: group={message.group_id}, "
            f"tokens={await self.redis.get_tokens(message.group_id)}"
        )

    async def _handle_interrupt_signal(
        self,
        message: MessageSchema,
        score: float,
    ) -> str | None:
        """处理中断信号（主动回复）。

        Args:
            message: 消息对象
            score: 评分

        Returns:
            回复内容
        """
        # 获取最新配置
        config = get_config()

        # 检查是否启用主动回复
        if not config.proactive_enabled:
            return None

        # 检查冷却
        if await self.redis.is_on_cooldown(message.group_id):
            logger.debug("[KomariMemory] 主动回复冷却中")
            return None

        # 检查频率限制
        current_count = await self.redis.get_proactive_count(message.group_id)
        if current_count >= config.proactive_max_per_hour:
            logger.debug("[KomariMemory] 主动回复频率超限")
            return None

        # 检索相关记忆
        memories = await self.memory.search_conversations(
            query=message.content,
            group_id=message.group_id,
            limit=3,
        )

        # 构建提示词
        prompt = await build_prompt(
            user_message=message.content,
            memories=memories,
            config=config,
        )

        # 生成回复
        reply = await generate_reply(
            user_message=message.content,
            system_prompt=prompt,
            config=config,
        )

        # 只有成功生成回复时才设置冷却和增加计数
        if reply is not None:
            await self.redis.set_cooldown(message.group_id, config.proactive_cooldown)
            await self.redis.increment_proactive_count(message.group_id)

            logger.info(
                f"[KomariMemory] 主动回复: group={message.group_id}, score={score:.2f}"
            )
        else:
            logger.warning(
                f"[KomariMemory] 主动回复生成失败: group={message.group_id}, score={score:.2f}"
            )

        return reply


# 简单的 token 估算函数
def estimate_tokens(text: str) -> int:
    """估算文本的 token 数量。

    Args:
        text: 输入文本

    Returns:
        估算的 token 数量
    """
    # 简化处理：中文约 1.5 字 = 1 token，英文约 4 字 = 1 token
    # 这里取平均值，约为 2 字 = 1 token
    return max(1, len(text) // 2)
