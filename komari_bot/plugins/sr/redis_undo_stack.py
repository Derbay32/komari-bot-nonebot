"""基于 Redis 的撤销栈实现。"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, TypedDict, cast
from uuid import uuid4

import redis.asyncio as aioredis

from komari_bot.common.redis_config import get_shared_redis_config

if TYPE_CHECKING:
    from collections.abc import Awaitable

_redis_client: aioredis.Redis | None = None
_redis_client_lock: asyncio.Lock | None = None
_redis_client_lock_loop: asyncio.AbstractEventLoop | None = None


class UndoCommandData(TypedDict):
    """Redis 撤销栈中的命令快照。"""

    token: str
    type: str
    item: str | None
    index: int | None


_REPLACE_HEAD_SCRIPT = """
local current = redis.call('LINDEX', KEYS[1], 0)
if current == ARGV[1] then
    redis.call('LSET', KEYS[1], 0, ARGV[2])
    return 1
end
return 0
"""

_POP_HEAD_BY_TOKEN_SCRIPT = """
local current = redis.call('LINDEX', KEYS[1], 0)
if not current then
    return 0
end
local ok, decoded = pcall(cjson.decode, current)
if not ok or type(decoded) ~= 'table' or decoded['token'] ~= ARGV[1] then
    return 0
end
redis.call('LPOP', KEYS[1])
return 1
"""


def _get_redis_client_lock() -> asyncio.Lock:
    """获取绑定到当前事件循环的 Redis 客户端锁。"""
    global _redis_client_lock, _redis_client_lock_loop  # noqa: PLW0603
    loop = asyncio.get_running_loop()
    if _redis_client_lock is None or _redis_client_lock_loop is not loop:
        _redis_client_lock = asyncio.Lock()
        _redis_client_lock_loop = loop
    return _redis_client_lock


def _is_redis_success(value: object) -> bool:
    """将 Redis 脚本的整数或文本响应安全转换为成功标志。"""
    if isinstance(value, bytes):
        return value == b"1"
    if isinstance(value, str):
        return value == "1"
    return isinstance(value, int) and value == 1


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
            db_config = get_shared_redis_config()
            _redis_client = aioredis.Redis(
                host=db_config.redis_host,
                port=db_config.redis_port,
                db=config.redis_db,
                password=db_config.redis_password or None,
                decode_responses=True,
                encoding="utf-8",
            )
    return _redis_client


async def push_undo(user_id: str, command: Any, config_manager: Any) -> str:
    """将命令压入用户撤销栈（最多5条，TTL 12小时）。

    Args:
        user_id: 用户 ID
        command: 命令对象（AddCommand 或 DeleteCommand）
        config_manager: 配置管理器实例
    """
    client = await get_redis_client(config_manager)
    key = f"sr:undo:{user_id}"

    # 序列化命令
    token = uuid4().hex
    cmd_data: UndoCommandData = {
        "token": token,
        "type": command.__class__.__name__,
        "item": command.item if hasattr(command, "item") else None,
        "index": command.index if hasattr(command, "index") else None,
    }

    pipe = client.pipeline()
    pipe.lpush(
        key,
        json.dumps(cmd_data, ensure_ascii=False, separators=(",", ":")),
    )
    pipe.ltrim(key, 0, 4)  # 保留最新的 5 条
    pipe.expire(key, 43200)  # 12 小时 TTL
    await pipe.execute()
    return token


def _decode_payload(data: object) -> tuple[str, dict[str, object]] | None:
    """解析 Redis 中的撤销载荷，不接受非对象 JSON。"""
    try:
        raw_text = data.decode() if isinstance(data, bytes) else str(data)
        decoded = json.loads(raw_text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, dict):
        return None
    return raw_text, decoded


def _build_command_data(
    decoded: dict[str, object],
    *,
    token: str,
) -> UndoCommandData | None:
    """校验撤销载荷并转换为稳定类型。"""
    command_type = decoded.get("type")
    if not isinstance(command_type, str) or command_type not in {
        "AddCommand",
        "DeleteCommand",
    }:
        return None

    raw_item = decoded.get("item")
    if not isinstance(raw_item, str) or not raw_item:
        return None

    raw_index = decoded.get("index")
    if isinstance(raw_index, bool):
        return None
    try:
        index = int(raw_index) if isinstance(raw_index, int | str) else None
    except ValueError:
        return None
    if index is not None and index < 1:
        return None

    return UndoCommandData(
        token=token,
        type=command_type,
        item=raw_item,
        index=index,
    )


async def peek_undo(user_id: str, config_manager: Any) -> UndoCommandData | None:
    """读取但不移除用户撤销栈顶。

    旧版无 token 的栈顶会通过 Redis CAS 原地补齐 token，避免升级后直接
    丢掉最长 12 小时内仍有效的撤销记录。
    """
    client = await get_redis_client(config_manager)
    key = f"sr:undo:{user_id}"

    for _ in range(3):
        raw = await client.lindex(key, 0)  # type: ignore[misc]
        if raw is None:
            return None
        parsed = _decode_payload(raw)
        if parsed is None:
            return None
        raw_text, decoded = parsed

        stored_token = decoded.get("token")
        if isinstance(stored_token, str) and stored_token:
            return _build_command_data(decoded, token=stored_token)

        migrated_token = uuid4().hex
        migrated_payload = dict(decoded)
        migrated_payload["token"] = migrated_token
        migrated_text = json.dumps(
            migrated_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        replaced = await cast(
            "Awaitable[object]",
            client.eval(
                _REPLACE_HEAD_SCRIPT,
                1,
                key,
                raw_text,
                migrated_text,
            ),
        )
        if _is_redis_success(replaced):
            return _build_command_data(migrated_payload, token=migrated_token)
    return None


async def pop_undo_if_token(
    user_id: str,
    token: str,
    config_manager: Any,
) -> bool:
    """仅当栈顶 token 未变化时原子弹出撤销记录。"""
    client = await get_redis_client(config_manager)
    key = f"sr:undo:{user_id}"
    popped = await cast(
        "Awaitable[object]",
        client.eval(_POP_HEAD_BY_TOKEN_SCRIPT, 1, key, token),
    )
    return _is_redis_success(popped)


async def pop_undo(user_id: str, config_manager: Any) -> UndoCommandData | None:
    """兼容旧调用：以 token 化 CAS 方式弹出一个命令。

    Args:
        user_id: 用户 ID
        config_manager: 配置管理器实例

    Returns:
        命令字典，如果栈为空则返回 None
    """
    command = await peek_undo(user_id, config_manager)
    if command is None:
        return None
    if not await pop_undo_if_token(user_id, command["token"], config_manager):
        return None
    return command


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
