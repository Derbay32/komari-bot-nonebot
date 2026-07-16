"""基于 Redis 的撤销栈实现。"""

from __future__ import annotations

import asyncio
import json
from typing import Any, TypedDict

import redis.asyncio as aioredis

from komari_bot.common.database_config import get_shared_database_config

_redis_client: aioredis.Redis | None = None
_redis_client_lock: asyncio.Lock | None = None
_redis_client_lock_loop: asyncio.AbstractEventLoop | None = None


class UndoCommandData(TypedDict):
    """Redis 撤销栈中的命令快照。"""

    type: str
    item: str | None
    index: int | None


def _get_redis_client_lock() -> asyncio.Lock:
    """获取绑定到当前事件循环的 Redis 客户端锁。"""
    global _redis_client_lock, _redis_client_lock_loop  # noqa: PLW0603
    loop = asyncio.get_running_loop()
    if _redis_client_lock is None or _redis_client_lock_loop is not loop:
        _redis_client_lock = asyncio.Lock()
        _redis_client_lock_loop = loop
    return _redis_client_lock


async def get_redis_config(config_manager: Any) -> Any:
    """从 config_manager 获取插件 Redis 配置。

    Args:
        config_manager: 配置管理器实例

    Returns:
        配置对象，包含插件级 Redis 配置
    """
    return await config_manager.get_async()


async def get_redis_client(config_manager: Any) -> aioredis.Redis:
    """获取 Redis 客户端（单例模式）。

    Args:
        config_manager: 配置管理器实例

    Returns:
        Redis 客户端实例
    """
    global _redis_client  # noqa: PLW0603
    if _redis_client is not None:
        return _redis_client

    async with _get_redis_client_lock():
        if _redis_client is None:
            config = await get_redis_config(config_manager)
            db_config = get_shared_database_config()
            _redis_client = aioredis.Redis(
                host=db_config.redis_host,
                port=db_config.redis_port,
                db=config.redis_db,
                password=db_config.redis_password or None,
                decode_responses=True,
                encoding="utf-8",
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


async def pop_undo(user_id: str, config_manager: Any) -> UndoCommandData | None:
    """从用户撤销栈弹出一个命令。

    Args:
        user_id: 用户 ID
        config_manager: 配置管理器实例

    Returns:
        命令字典，如果栈为空则返回 None
    """
    client = await get_redis_client(config_manager)
    key = f"sr:undo:{user_id}"
    data = await client.lpop(key)  # type: ignore[misc]
    if data is None:
        return None
    try:
        raw_text = data.decode() if isinstance(data, bytes) else str(data)
        decoded = json.loads(raw_text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, dict):
        return None
    command_type = decoded.get("type")
    if command_type not in {"AddCommand", "DeleteCommand"}:
        return None
    raw_index = decoded.get("index")
    try:
        index = int(raw_index) if isinstance(raw_index, int | str) else None
    except ValueError:
        return None
    raw_item = decoded.get("item")
    return UndoCommandData(
        type=command_type,
        item=str(raw_item) if raw_item is not None else None,
        index=index,
    )


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
    async with _get_redis_client_lock():
        client = _redis_client
        _redis_client = None
        if client is not None:
            await client.aclose()
