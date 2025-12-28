"""
配置管理插件 - 通用配置管理功能。

提供：
- 从 JSON/.env 分层加载配置
- 运行时配置更新并持久化
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
config = config_manager.reload_from_json()
```
"""
from nonebot.plugin import PluginMetadata

from .manager import ConfigManager, get_config_manager

__plugin_meta__ = PluginMetadata(
    name="config_manager",
    description="通用配置管理插件，提供 JSON/.env 分层配置加载和运行时更新",
    usage="详见插件文档",
)

__all__ = [
    "ConfigManager",
    "get_config_manager",
]
