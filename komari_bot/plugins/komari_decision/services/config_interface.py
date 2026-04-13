"""Komari Decision 配置接口。"""

from nonebot.plugin import require

from ..config_schema import KomariDecisionConfigSchema

config_manager_plugin = require("config_manager")

_config_manager = config_manager_plugin.get_config_manager(
    "komari_decision", KomariDecisionConfigSchema
)


def get_config() -> KomariDecisionConfigSchema:
    """获取当前配置。"""
    return _config_manager.get()


__all__ = ["get_config"]
