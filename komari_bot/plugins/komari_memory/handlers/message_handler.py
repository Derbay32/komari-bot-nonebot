"""Komari Memory 消息处理核心。"""

import time

from nonebot import logger
from nonebot.adapters.onebot.v11 import GroupMessageEvent

from .. import get_config
from ..services.bert_client import score_message
from ..services.llm_service import generate_reply
from ..services.memory_service import MemoryService
from ..services.message_filter import preprocess_message
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
    ) -> str | None:
        """处理群聊消息的主流程。

        Args:
            event: 群聊消息事件

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

        # 前置过滤
        filter_result = await preprocess_message(
            message=message_content,
            config=config,
            redis=self.redis,
            group_id=group_id,
        )

        if filter_result.should_skip:
            logger.debug(
                f"[KomariMemory] 消息被过滤: {filter_result.reason} - "
                f"{message_content[:30]}..."
            )
            # 低价值消息直接丢弃
            await self._handle_low_value(message)
            return None

        # 调用 BERT 服务评分
        score = await score_message(
            message=message_content,
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
        """处理低价值消息（直接丢弃，不存储）。

        Args:
            message: 消息对象
        """
        # 低价值消息不存储到缓冲区，节省空间
        logger.debug(f"[KomariMemory] 低价值消息已丢弃: {message.content[:30]}...")

    async def _handle_normal_message(self, message: MessageSchema) -> None:
        """处理普通消息。

        Args:
            message: 消息对象
        """
        # 存入 Redis 并计入消息数和 token
        await self.redis.push_message(message.group_id, message)
        await self.redis.increment_message_count(message.group_id)

        # 同时统计 token（作为备用触发条件）
        token_count = len(message.content)
        await self.redis.increment_tokens(message.group_id, token_count)

        logger.debug(
            f"[KomariMemory] 消息已存储: group={message.group_id}, "
            f"messages={await self.redis.get_message_count(message.group_id)}, "
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
            limit=config.memory_search_limit,
        )

        # 获取最近的消息上下文
        recent_messages = await self.redis.get_buffer(message.group_id, limit=config.context_messages_limit)

        # 构建提示词（返回 system_prompt 和 user_context）
        system_prompt, user_context = await build_prompt(
            user_message=message.content,
            memories=memories,
            config=config,
            recent_messages=recent_messages,
        )

        # 生成回复（user_context 已包含记忆、常识库、用户输入）
        reply = await generate_reply(
            user_message=user_context,
            system_prompt=system_prompt,
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
