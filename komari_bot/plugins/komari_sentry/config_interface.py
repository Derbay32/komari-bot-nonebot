"""Komari Sentry 配置接口。"""

from __future__ import annotations

from nonebot.plugin import require

from .config_schema import KomariSentryConfigSchema

config_manager_plugin = require("config_manager")

_config_manager = config_manager_plugin.get_config_manager(
    "komari_sentry", KomariSentryConfigSchema
)


def get_config() -> KomariSentryConfigSchema:
    """获取当前配置。"""
    return _config_manager.get()

