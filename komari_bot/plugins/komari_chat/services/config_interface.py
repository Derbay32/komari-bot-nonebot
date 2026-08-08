"""Komari Chat 配置接口 - 与 config_manager 插件通信的中间层。

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

# 导入配置 Schema（绝对导入：config_schema 是 SQLModel 表模型，必须始终
# 以唯一真实模块名加载一次；相对导入在测试把插件入口加载为别名模块时
# 会导致同表重复注册）
from komari_bot.plugins.komari_chat.config_schema import KomariChatConfigSchema
from komari_bot.plugins.komari_memory.config_schema import KomariMemoryConfigSchema

# 获取配置管理器（插件级单例；komari_memory 资源与 komari_memory 自身
# 接口共用同一工厂注册表，不产生第二份缓存）
_config_manager = config_manager_plugin.get_config_manager(
    "komari_chat", KomariChatConfigSchema
)
_memory_config_manager = config_manager_plugin.get_config_manager(
    "komari_memory", KomariMemoryConfigSchema
)


def get_config() -> KomariChatConfigSchema:
    """获取当前配置（自动检测文件变化）。

    Returns:
        当前配置对象
    """
    return cast("KomariChatConfigSchema", _config_manager.get())


async def get_config_async() -> KomariChatConfigSchema:
    """在事件循环内异步获取当前配置。"""
    return cast("KomariChatConfigSchema", await _config_manager.get_async())


def get_memory_config() -> KomariMemoryConfigSchema:
    """获取 komari_memory 配置（聊天流程仍依赖的 memory 侧字段）。"""
    return cast("KomariMemoryConfigSchema", _memory_config_manager.get())


async def get_memory_config_async() -> KomariMemoryConfigSchema:
    """在事件循环内异步获取 komari_memory 配置。"""
    return cast(
        "KomariMemoryConfigSchema", await _memory_config_manager.get_async()
    )


__all__ = [
    "get_config",
    "get_config_async",
    "get_memory_config",
    "get_memory_config_async",
]
