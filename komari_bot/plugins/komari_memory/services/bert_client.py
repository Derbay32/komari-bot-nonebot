"""Komari Memory BERT 评分服务客户端。"""

import httpx

from ..config_schema import KomariMemoryConfigSchema
from ..core.retry import retry_async


@retry_async(max_attempts=3, base_delay=0.5, exceptions=(Exception,))
async def score_message(
    message: str,
    user_id: str,
    group_id: str,
    config: KomariMemoryConfigSchema,
) -> float:
    """调用 BERT 评分服务（带重试机制）。

    Args:
        message: 当前消息内容
        user_id: 发送者 ID
        group_id: 群组 ID
        config: 插件配置

    Returns:
        评分结果 (0.0 - 1.0)
    """
    async with httpx.AsyncClient(timeout=config.bert_timeout) as client:
        response = await client.post(
            config.bert_service_url,
            json={
                "message": message,
                "context": "",
                "user_id": user_id,
                "group_id": group_id,
            },
        )
        response.raise_for_status()
        data = response.json()
        return float(data.get("score", 0.5))
