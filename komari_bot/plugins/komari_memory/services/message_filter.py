"""消息预过滤器 - BERT评分前的快速过滤层。"""

import re
from dataclasses import dataclass
from typing import Literal

from nonebot import logger

from ..config_schema import KomariMemoryConfigSchema
from ..services.redis_manager import RedisManager


@dataclass(frozen=True)
class FilterResult:
    """过滤结果。"""

    should_skip: bool
    reason: Literal["short", "history_repeat", "none", "command"]

    def __init__(
        self,
        *,
        should_skip: bool,
        reason: Literal["short", "history_repeat", "none", "command"],
    ) -> None:
        """初始化过滤结果（强制使用关键字参数）。

        Args:
            should_skip: 是否应该跳过BERT评分
            reason: 过滤原因
        """
        object.__setattr__(self, "should_skip", should_skip)
        object.__setattr__(self, "reason", reason)


def _is_command_message(message: str) -> bool:
    """检查消息是否为命令消息（以 . 或 。开头）。

    Args:
        message: 原始消息内容

    Returns:
        是否为命令消息
    """
    # 使用 regex 匹配以 . 或 。开头的消息
    # 匹配开头可能有的空白字符，然后是 . 或 。，之后至少有一个非空白字符
    pattern = r"^\s*[.。]\S"

    return bool(re.match(pattern, message))


async def preprocess_message(
    message: str,
    config: KomariMemoryConfigSchema,
    redis: RedisManager,
    group_id: str,
) -> FilterResult:
    """在调用BERT评分前预处理消息。

    Args:
        message: 当前消息内容
        config: 插件配置
        redis: Redis管理器
        group_id: 群组ID

    Returns:
        过滤结果对象
    """
    # 0. 命令消息过滤（以 . 或 。 开头）
    if _is_command_message(message):
        logger.debug(f"[KomariMemory] 过滤命令消息: {message[:50]}...")
        return FilterResult(should_skip=True, reason="command")

    # 1. 极短文本过滤
    if len(message.strip()) < config.filter_min_length:
        return FilterResult(should_skip=True, reason="short")

    # 2. 历史重复检测
    if await _check_history_repeat(
        message=message,
        redis=redis,
        group_id=group_id,
        check_size=config.filter_history_check_size,
    ):
        return FilterResult(should_skip=True, reason="history_repeat")

    return FilterResult(should_skip=False, reason="none")


async def _check_history_repeat(
    message: str,
    redis: RedisManager,
    group_id: str,
    check_size: int,
) -> bool:
    """检查消息是否在历史记录中出现过。

    Args:
        message: 当前消息
        redis: Redis管理器
        group_id: 群组ID
        check_size: 检查最近N条消息

    Returns:
        是否重复
    """
    recent_messages = await redis.get_buffer(group_id, limit=check_size)
    message_clean = message.strip().lower()

    return any(msg.content.strip().lower() == message_clean for msg in recent_messages)
