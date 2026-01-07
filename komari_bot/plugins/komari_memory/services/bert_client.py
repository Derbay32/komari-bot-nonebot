"""Komari Memory BERT 评分服务客户端。"""

import asyncio

import httpx
from nonebot import logger

from ..config_schema import KomariMemoryConfigSchema


async def score_message(
    message: str,
    context: str,
    user_id: str,
    group_id: str,
    config: KomariMemoryConfigSchema,
) -> float:
    """调用 BERT 评分服务（带重试机制）。

    Args:
        message: 当前消息内容
        context: 上一句消息内容
        user_id: 发送者 ID
        group_id: 群组 ID
        config: 插件配置

    Returns:
        评分结果 (0.0 - 1.0)
    """
    last_error = None

    for attempt in range(3):  # 总共尝试 3 次
        try:
            async with httpx.AsyncClient(timeout=config.bert_timeout) as client:
                response = await client.post(
                    config.bert_service_url,
                    json={
                        "message": message,
                        "context": context,
                        "user_id": user_id,
                        "group_id": group_id,
                    },
                )
                response.raise_for_status()
                data = response.json()
                score = float(data.get("score", 0.5))
                logger.debug(f"[BERTClient] 评分成功 (尝试 {attempt + 1}/3)")
                return score

        except Exception as e:
            last_error = e
            if attempt < 2:  # 前 2 次失败后重试
                logger.warning(
                    f"[BERTClient] 第 {attempt + 1} 次尝试失败: {e}，重试中..."
                )
                await asyncio.sleep(0.5 * (attempt + 1))  # 指数退避
            else:
                logger.error(f"[BERTClient] 3 次尝试全部失败: {last_error}")

    # 所有尝试失败，使用默认值
    return 0.5
