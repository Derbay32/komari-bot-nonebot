"""Komari Memory 后台总结任务。"""

import asyncio

from nonebot import logger
from nonebot_plugin_apscheduler import scheduler

from .. import get_config
from ..services.llm_service import summarize_conversation
from ..services.memory_service import MemoryService
from ..services.redis_manager import RedisManager


async def summary_worker_task(
    redis: RedisManager,
    memory: MemoryService,
) -> None:
    """定期检查并触发总结。

    Args:
        redis: Redis 管理器
        memory: 记忆服务
    """

    # 获取所有有消息缓冲的群组
    group_ids = await redis.get_active_groups()

    if not group_ids:
        return

    logger.debug(f"[KomariMemory] 检查 {len(group_ids)} 个群组的总结任务...")

    for group_id in group_ids:
        if await redis.should_trigger_summary(group_id):
            # 重试机制：总共尝试 3 次
            last_error = None
            for attempt in range(3):
                try:
                    await perform_summary(group_id, redis, memory)
                    break  # 成功则跳出重试循环
                except Exception as e:
                    last_error = e
                    if attempt < 2:  # 前 2 次失败后重试
                        logger.warning(
                            f"[KomariMemory] 群组 {group_id} 总结第 {attempt + 1} 次失败: {e}，重试中..."
                        )
                        await asyncio.sleep(1.0 * (attempt + 1))  # 指数退避
                    else:
                        logger.error(
                            f"[KomariMemory] 群组 {group_id} 总结 3 次全部失败: {last_error}，等待下次触发"
                        )


async def perform_summary(
    group_id: str,
    redis: RedisManager,
    memory: MemoryService,
) -> None:
    """执行群组的对话总结。

    Args:
        group_id: 群组 ID
        redis: Redis 管理器
        memory: 记忆服务
    """
    logger.info(f"[KomariMemory] 开始总结群组 {group_id} 的对话")

    # 获取最新配置
    config = get_config()

    # 获取消息缓冲
    messages_buffer = await redis.get_buffer(group_id, limit=200)

    if not messages_buffer:
        logger.warning(f"[KomariMemory] 群组 {group_id} 消息缓冲为空")
        return

    # 提取消息内容
    message_texts = [msg.content for msg in messages_buffer]

    # 调用 LLM 总结
    result = await summarize_conversation(message_texts, config)

    summary = result.get("summary", "")
    entities = result.get("entities", [])
    importance = result.get("importance", 3)

    if not summary:
        logger.warning(f"[KomariMemory] 群组 {group_id} 总结为空，跳过存储")
        return

    # 获取参与者列表
    participants = list({msg.user_id for msg in messages_buffer})

    # 存储对话总结（带向量和重要性评分）
    conversation_id = await memory.store_conversation(
        group_id=group_id,
        summary=summary,
        participants=participants,
        importance_initial=importance,
    )

    # 存储实体
    for entity in entities:
        try:
            await memory.upsert_entity(
                user_id=entity.get(
                    "user_id", participants[0] if participants else "unknown"
                ),
                group_id=group_id,
                key=entity.get("key", ""),
                value=entity.get("value", ""),
                category=entity.get("category", "general"),
                importance=entity.get("importance", 3),
            )
        except Exception as e:
            logger.debug(f"[KomariMemory] 存储实体失败: {e}")

    # 重置消息计数
    await redis.reset_message_count(group_id)

    # 重置 token 计数（保留向后兼容）
    await redis.reset_tokens(group_id)

    # 清空消息缓冲区
    await redis.delete_buffer(group_id)

    # 更新最后总结时间
    await redis.update_last_summary(group_id)

    logger.info(
        f"[KomariMemory] 群组 {group_id} 总结完成: "
        f"conversation_id={conversation_id}, entities={len(entities)}"
    )


# 注册定时任务的辅助函数
def register_summary_task(
    redis: RedisManager,
    memory: MemoryService,
) -> None:
    """注册总结定时任务。

    Args:
        redis: Redis 管理器
        memory: 记忆服务
    """
    scheduler.add_job(
        summary_worker_task,
        "interval",
        minutes=5,
        args=[redis, memory],
        id="komari_memory_summary_worker",
        replace_existing=True,
    )
    logger.info("[KomariMemory] 总结定时任务已注册")


# 取消注册定时任务的辅助函数
def unregister_summary_task() -> None:
    """取消注册总结定时任务。"""
    try:
        scheduler.remove_job("komari_memory_summary_worker")
        logger.info("[KomariMemory] 总结定时任务已取消")
    except Exception:
        pass
