"""Komari Sentry 配置接口。"""

from __future__ import annotations

from typing import cast

from nonebot.plugin import require

from .config_schema import KomariSentryConfigSchema

require("config_manager")
from komari_bot.plugins import config_manager as config_manager_plugin

_config_manager = config_manager_plugin.get_config_manager(
    "komari_sentry", KomariSentryConfigSchema
)


def get_config() -> KomariSentryConfigSchema:
    """获取当前配置。"""
    return cast("KomariSentryConfigSchema", _config_manager.get())


async def get_config_async() -> KomariSentryConfigSchema:
    """在事件循环内异步获取当前配置。"""
    return cast("KomariSentryConfigSchema", await _config_manager.get_async())


__all__ = ["get_config", "get_config_async"]
