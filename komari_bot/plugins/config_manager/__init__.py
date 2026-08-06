"""
配置管理插件 - 通用配置管理功能。

提供：
- 从 PostgreSQL 加载插件动态配置
- 首次缺失时从 .env 初始化并持久化
- 线程安全的配置访问
- 可扩展的配置 Schema

使用示例：
```python
from nonebot.plugin import require
from config_manager import get_config_manager
from config_manager.schemas import BaseConfigSchema

# 定义配置 Schema
class MyConfigSchema(BaseConfigSchema):
    api_key: str = ""
    timeout: int = 30

# 获取配置管理器
config_manager = get_config_manager("my_plugin", MyConfigSchema)
config = config_manager.initialize()

# 更新配置
config_manager.update_field("timeout", 60)

# 重新加载
config = config_manager.reload()
```
"""
import asyncio

from nonebot import get_driver
from nonebot.plugin import PluginMetadata, require

from komari_bot.common.prompt_storage import (
    close_prompt_storage_if_created,
    get_prompt_storage,
)

require("nonebot_plugin_orm")

from .manager import (
    ConfigManager,
    get_config_manager,
    get_registered_config_managers,
    initialize_registered_config_managers_async,
)
from .storage import close_config_storage_if_created, get_config_storage

__plugin_meta__ = PluginMetadata(
    name="config_manager",
    description="通用配置管理插件，提供 PostgreSQL 配置存储和运行时更新",
    usage="详见插件文档",
)

__all__ = [
    "ConfigManager",
    "get_config_manager",
    "get_registered_config_managers",
    "initialize_registered_config_managers_async",
]


driver = get_driver()


@driver.on_startup
async def _bind_config_storage_app_loop() -> None:
    """把应用事件循环交给配置存储，启动 revision 轮询任务。"""
    get_config_storage().bind_app_loop(asyncio.get_running_loop())


@driver.on_startup
async def _bind_prompt_storage_app_loop() -> None:
    """把应用事件循环交给 Prompt 存储，供同步桥跨线程提交使用。"""
    get_prompt_storage().bind_app_loop(asyncio.get_running_loop())


@driver.on_startup
async def _initialize_registered_configs() -> None:
    """先于依赖本插件的业务启动钩子异步预热配置。"""
    await initialize_registered_config_managers_async()


@driver.on_shutdown
async def _close_config_storage() -> None:
    """等待配置存储轮询任务退出，并关闭 Prompt 存储。"""
    close_prompt_storage_if_created()
    await close_config_storage_if_created()
