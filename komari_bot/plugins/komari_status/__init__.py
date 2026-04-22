"""Komari Status 状态查询插件。"""

from __future__ import annotations

import importlib

from nonebot import get_driver, logger
from nonebot.plugin import PluginMetadata, require

from .api_client import configure_status_client, get_status_client
from .config_schema import StatusConfig

config_manager_plugin = require("config_manager")

config_manager = config_manager_plugin.get_config_manager(
    "komari_status",
    StatusConfig,
)
status_client = configure_status_client(config_manager.get)

__plugin_meta__ = PluginMetadata(
    name="komari_status",
    description="查询 Uptime Kuma 监控状态、uptime 与维护计划",
    usage=".status — 查询当前服务状态概览、uptime 与维护计划",
    config=StatusConfig,
)

try:
    driver = get_driver()
except ValueError:
    driver = None
else:
    importlib.import_module("komari_bot.plugins.komari_status.commands")


def _has_complete_credentials(config: StatusConfig) -> bool:
    return bool(
        config.uptime_kuma_username.strip() and config.uptime_kuma_password.strip()
    )


async def on_startup() -> None:
    """插件启动时校验配置。"""
    config = config_manager.get()
    if not config.plugin_enable:
        logger.info("[Komari Status] 插件未启用，跳过初始化")
        return

    if not _has_complete_credentials(config):
        logger.warning("[Komari Status] 用户名或密码未配置，.status 命令将暂时不可用")
        return

    logger.info("[Komari Status] 插件启动完成")


async def on_shutdown() -> None:
    """插件关闭时清理缓存。"""
    status_client.clear_cache()
    logger.info("[Komari Status] 插件已关闭")


if driver is not None:

    @driver.on_startup
    async def _driver_startup() -> None:
        await on_startup()

    @driver.on_shutdown
    async def _driver_shutdown() -> None:
        await on_shutdown()


__plugin_startup__ = on_startup
__plugin_shutdown__ = on_shutdown

__all__ = [
    "StatusConfig",
    "config_manager",
    "get_status_client",
    "status_client",
]
