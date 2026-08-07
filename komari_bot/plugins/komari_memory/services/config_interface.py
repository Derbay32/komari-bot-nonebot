"""Komari Memory 配置接口 - 与 config_manager 插件通信的中间层。

此模块封装所有与 config_manager 插件的交互逻辑，
为插件内部提供统一的配置访问接口。

使用示例：
    from .config_interface import get_config
    config = get_config()
"""

from typing import cast

from nonebot.plugin import require

# 导入 config_manager 插件
require("config_manager")

from komari_bot.plugins import config_manager as config_manager_plugin

# 导入配置 Schema
from ..config_schema import KomariMemoryConfigSchema

# 获取配置管理器（插件级单例）
_config_manager = config_manager_plugin.get_config_manager(
    "komari_memory", KomariMemoryConfigSchema
)


def get_config() -> KomariMemoryConfigSchema:
    """获取当前配置（自动检测文件变化）。

    Returns:
        当前配置对象
    """
    return cast("KomariMemoryConfigSchema", _config_manager.get())


async def get_config_async() -> KomariMemoryConfigSchema:
    """在事件循环内异步获取当前配置。"""
    return cast("KomariMemoryConfigSchema", await _config_manager.get_async())


__all__ = ["get_config", "get_config_async"]
