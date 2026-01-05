"""基于 Redis 的撤销栈实现。"""

import json
from typing import Any

import redis.asyncio as aioredis

# Redis 客户端单例
_redis_client: aioredis.Redis | None = None


def get_redis_config(config_manager: Any) -> Any:
    """从 config_manager 获取配置（包含 Redis 连接信息）。

    Args:
        config_manager: 配置管理器实例

    Returns:
        配置对象，包含 Redis 连接信息
    """
    return config_manager.get()


async def get_redis_client(config_manager: Any) -> aioredis.Redis:
    """获取 Redis 客户端（单例模式）。

    Args:
        config_manager: 配置管理器实例

    Returns:
        Redis 客户端实例
    """
    global _redis_client  # noqa: PLW0603
    if _redis_client is None:
        config = get_redis_config(config_manager)

        # 构建连接 URL
        password_part = f":{config.redis_password}@" if config.redis_password else ""
        redis_url = f"redis://{password_part}{config.redis_host}:{config.redis_port}/{config.redis_db}"

        _redis_client = await aioredis.from_url(
            redis_url, decode_responses=True, encoding="utf-8"
        )
    return _redis_client


async def push_undo(user_id: str, command: Any, config_manager: Any) -> None:
    """将命令压入用户撤销栈（最多5条，TTL 12小时）。

    Args:
        user_id: 用户 ID
        command: 命令对象（AddCommand 或 DeleteCommand）
        config_manager: 配置管理器实例
    """
    client = await get_redis_client(config_manager)
    key = f"sr:undo:{user_id}"

    # 序列化命令
    cmd_data = {
        "type": command.__class__.__name__,
        "item": command.item if hasattr(command, "item") else None,
        "index": command.index if hasattr(command, "index") else None,
    }

    pipe = client.pipeline()
    pipe.lpush(key, json.dumps(cmd_data))
    pipe.ltrim(key, 0, 4)  # 保留最新的 5 条
    pipe.expire(key, 43200)  # 12 小时 TTL
    await pipe.execute()


async def pop_undo(user_id: str, config_manager: Any) -> dict | None:
    """从用户撤销栈弹出一个命令。

    Args:
        user_id: 用户 ID
        config_manager: 配置管理器实例

    Returns:
        命令字典，如果栈为空则返回 None
    """
    client = await get_redis_client(config_manager)
    key = f"sr:undo:{user_id}"
    data = await client.lpop(key)  # type: ignore[misc] - 谁家 lpop 不是异步啊为什么说我类型检查不过
    if data is None:
        return None
    return json.loads(data)  # type: ignore[arg-type] - 反正不是空


async def clear_undo(user_id: str, config_manager: Any) -> None:
    """清空用户撤销栈。

    Args:
        user_id: 用户 ID
        config_manager: 配置管理器实例
    """
    client = await get_redis_client(config_manager)
    await client.delete(f"sr:undo:{user_id}")


async def close_redis() -> None:
    """关闭 Redis 连接。"""
    global _redis_client  # noqa: PLW0603
    if _redis_client:
        await _redis_client.close()
        _redis_client = None
