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
from nonebot import get_driver
from nonebot.plugin import PluginMetadata

from komari_bot.common.prompt_storage import close_prompt_storage_if_created

from .manager import (
    ConfigManager,
    get_config_manager,
    initialize_registered_config_managers_async,
)
from .storage import close_config_storage_if_created

__plugin_meta__ = PluginMetadata(
    name="config_manager",
    description="通用配置管理插件，提供 PostgreSQL 配置存储和运行时更新",
    usage="详见插件文档",
)

__all__ = [
    "ConfigManager",
    "get_config_manager",
    "initialize_registered_config_managers_async",
]


driver = get_driver()


@driver.on_startup
async def _initialize_registered_configs() -> None:
    """先于依赖本插件的业务启动钩子异步预热配置。"""
    await initialize_registered_config_managers_async()


@driver.on_shutdown
def _close_config_storage() -> None:
    """关闭已创建的配置与 Prompt 存储。"""
    close_prompt_storage_if_created()
    close_config_storage_if_created()
