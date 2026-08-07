"""Komari Decision 配置接口。"""

from typing import cast

from nonebot.plugin import require

require("config_manager")

from komari_bot.plugins import config_manager as config_manager_plugin

from ..config_schema import KomariDecisionConfigSchema

_config_manager = config_manager_plugin.get_config_manager(
    "komari_decision", KomariDecisionConfigSchema
)


def get_config() -> KomariDecisionConfigSchema:
    """获取当前配置。"""
    return cast("KomariDecisionConfigSchema", _config_manager.get())


async def get_config_async() -> KomariDecisionConfigSchema:
    """在事件循环内异步获取当前配置。"""
    return cast("KomariDecisionConfigSchema", await _config_manager.get_async())


__all__ = ["get_config", "get_config_async"]
