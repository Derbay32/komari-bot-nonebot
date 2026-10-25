"""Komari Context: sole owner of the recent-context Redis resource."""

from __future__ import annotations

from typing import Any, cast

import redis.asyncio as aioredis
from nonebot import get_driver, logger
from nonebot.plugin import PluginMetadata, require

require("config_manager")

from komari_bot.config.redis_config import get_shared_redis_config
from komari_bot.context.redis_store import MessageSchema, RecentContextStore
from komari_bot.plugins import config_manager as config_manager_plugin

from .config_schema import KomariContextConfigSchema

__plugin_meta__ = PluginMetadata(
    name="小鞠上下文",
    description="最近聊天上下文与聊天履约证据的 Redis 资源所有权",
    usage="自动运行，无需命令",
)

config_manager = config_manager_plugin.get_config_manager(
    "komari_context",
    KomariContextConfigSchema,
)

class PluginState:
    """Owned references for the single context Redis resource."""

    def __init__(self) -> None:
        self.store: RecentContextStore | None = None
        self.client: aioredis.Redis | None = None


state = PluginState()


def get_context_store() -> RecentContextStore | None:
    """Return the initialized recent-context store, if the client is ready."""
    return state.store


def _create_redis_client(redis_db: int) -> aioredis.Redis:
    db_config = get_shared_redis_config()
    return aioredis.Redis(
        host=db_config.redis_host,
        port=db_config.redis_port,
        db=redis_db,
        password=db_config.redis_password or None,
        decode_responses=True,
        encoding="utf-8",
    )


async def _initialize_context() -> None:
    if state.store is not None:
        return

    config = cast(
        "KomariContextConfigSchema", await config_manager.get_async()
    )
    client: aioredis.Redis | None = None
    try:
        client = _create_redis_client(int(config.redis_db))
        await cast("Any", client.ping())
        store = RecentContextStore(client)
    except BaseException:
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                logger.error("[KomariContext] 初始化失败后的连接关闭失败")
        raise

    state.client = client
    state.store = store
    logger.info("[KomariContext] Redis 连接已建立")


async def _shutdown_context() -> None:
    client = state.client
    state.client = None
    state.store = None
    if client is not None:
        try:
            await client.aclose()
        except Exception:
            logger.error("[KomariContext] Redis 连接关闭失败")
        else:
            logger.info("[KomariContext] Redis 连接已关闭")


@get_driver().on_startup
async def _startup_context() -> None:
    """Initialize the owned client without leaving a partial store behind."""
    try:
        await _initialize_context()
    except Exception as exc:
        logger.error(
            "[KomariContext] 初始化失败: error_type={}",
            type(exc).__name__,
        )


@get_driver().on_shutdown
async def _shutdown_context_hook() -> None:
    """Release exactly the client owned by this plugin."""
    await _shutdown_context()


__all__ = ["MessageSchema", "RecentContextStore", "get_context_store"]
