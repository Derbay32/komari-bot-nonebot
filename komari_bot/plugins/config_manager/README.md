# Config Manager 插件

通用配置管理插件，提供分层配置加载和运行时更新功能。

## 功能特性

- **分层配置加载**：JSON 文件 > .env 文件 > 默认值
- **运行时更新**：支持动态修改配置并持久化到 JSON
- **线程安全**：使用锁机制保证多线程环境下的配置访问安全
- **通用设计**：接受任何 Pydantic BaseModel 子类作为配置 Schema

## 安装

将插件放置在 `komari_bot/plugins/config_manager/` 目录下。

确保已安装依赖：
```bash
nb plugin install nonebot-plugin-localstore
```

## 使用方法

### 1. 定义配置 Schema

在你的插件中创建一个 Pydantic 配置类。参考 `example_schema.py`：

```python
from datetime import datetime
from pydantic import BaseModel, Field, field_validator

class DynamicConfigSchema(BaseModel):
    """我的插件配置"""

    # 元数据
    version: str = Field(default="1.0", description="配置架构版本")
    last_updated: str = Field(
        default_factory=lambda: datetime.now().isoformat(),
        description="最后更新时间戳"
    )

    # 插件控制
    plugin_enable: bool = Field(default=False, description="插件开关")

    # 白名单配置
    user_whitelist: list[str] = Field(
        default_factory=list,
        description="用户白名单，为空则允许所有用户"
    )
    group_whitelist: list[str] = Field(
        default_factory=list,
        description="群聊白名单，为空则允许所有群聊"
    )

    # 自定义字段
    api_key: str = Field(default="", description="API 密钥")
    timeout: int = Field(default=30, description="超时时间（秒）")

    @field_validator("user_whitelist", "group_whitelist", mode="before")
    @classmethod
    def parse_list_string(cls, v):
        """处理从 .env 格式解析列表。"""
        if isinstance(v, str):
            import json
            try:
                parsed = json.loads(v)
                return [str(item) for item in parsed]
            except (json.JSONDecodeError, TypeError):
                return [item.strip() for item in v.split(",") if item.strip()]
        return v
```

### 2. 在插件中使用

```python
from nonebot.plugin import PluginMetadata, require
from .config_schema import DynamicConfigSchema

# 依赖配置管理插件
config_manager_plugin = require("config_manager")

# 定义配置 Schema（见上面）
class DynamicConfigSchema(BaseModel):
    ...

# 获取配置管理器
config_manager = get_config_manager("my_plugin", MyPluginConfig)
config = config_manager.initialize()

# 获取当前配置
current_config = config_manager.get()
print(f"API Key: {current_config.api_key}")
print(f"插件状态: {'启用' if current_config.plugin_enable else '禁用'}")

# 更新配置
config_manager.update_field("timeout", 60)
config_manager.update_field("plugin_enable", True)

# 重新加载配置（从文件）
config = config_manager.reload_from_json()
```

### 3. 配合 .env 使用

在 `.env` 文件中配置默认值：

```env
# my_plugin 配置
my_plugin_plugin_enable = true
my_plugin_user_whitelist = ["123456", "789012"]
my_plugin_group_whitelist = ["111", "222"]
my_plugin_api_key = "your_api_key_here"
my_plugin_timeout = 30
```

### 4. 运行时配置文件

首次运行后，配置会自动保存到 `data/plugin_config/my_plugin_config.json`：

```json
{
  "version": "1.0",
  "last_updated": "2024-12-25T01:00:00",
  "plugin_enable": true,
  "user_whitelist": ["123456", "789012"],
  "group_whitelist": ["111", "222"],
  "api_key": "your_api_key_here",
  "timeout": 30
}
```

## API 参考

### ConfigManager

#### `get_config_manager(plugin_name: str, config_schema: Type[BaseModel]) -> ConfigManager`

获取配置管理器单例实例。

**参数：**
- `plugin_name`: 插件名称（用于配置文件命名）
- `config_schema`: 配置 Schema 类（Pydantic BaseModel 子类）

**返回：** `ConfigManager` 实例

#### `initialize() -> BaseModel`

初始化配置，从 JSON 或 .env 加载。

**返回：** 配置 Schema 实例

#### `get() -> BaseModel`

获取当前的动态配置。

**返回：** 当前配置 Schema 实例

#### `update_field(field_name: str, value: Any) -> BaseModel`

更新单个配置字段并持久化到 JSON。

**参数：**
- `field_name`: 字段名称
- `value`: 新值

**返回：** 更新后的配置 Schema 实例

**抛出：** `ValueError` 如果字段不存在

#### `reload_from_json() -> BaseModel`

从 JSON 文件重新加载配置。

**返回：** 重新加载的配置 Schema 实例

## 注意事项

1. **Schema 定义**：每个插件需要自己定义配置 Schema，参考 `example_schema.py`
2. **单例模式**：相同 `plugin_name` 和 `config_schema` 的组合会返回同一个实例
3. **配置文件位置**：使用 `nonebot_plugin_localstore` 管理配置文件路径
4. **文件权限**：JSON 配置文件权限设置为 `0600`（仅用户可读写）
5. **线程安全**：所有配置操作都是线程安全的
6. **字段验证**：更新配置时会通过 Pydantic 进行验证

## 完整示例

```python
"""my_plugin/__init__.py"""
from datetime import datetime
from typing import List
from nonebot.plugin import PluginMetadata, require
from nonebot import on_command
from nonebot.params import CommandArg
from nonebot.adapters.onebot.v11 import Message, MessageEvent

from pydantic import BaseModel, Field, field_validator
from config_manager import get_config_manager
from permission_manager import check_runtime_permission

require("config_manager")

class MyConfig(BaseModel):
    version: str = Field(default="1.0")
    last_updated: str = Field(default_factory=lambda: datetime.now().isoformat())
    plugin_enable: bool = Field(default=False)
    user_whitelist: List[str] = Field(default_factory=list)
    group_whitelist: List[str] = Field(default_factory=list)
    api_key: str = ""
    timeout: int = 30

    @field_validator("user_whitelist", "group_whitelist", mode="before")
    @classmethod
    def parse_list_string(cls, v):
        if isinstance(v, str):
            import json
            try:
                return [str(i) for i in json.loads(v)]
            except (json.JSONDecodeError, TypeError):
                return [i.strip() for i in v.split(",") if i.strip()]
        return v

config_manager = get_config_manager("my_plugin", MyConfig)
config = config_manager.initialize()

__plugin_meta__ = PluginMetadata(
    name="my_plugin",
    description="我的插件",
    usage="/my_config - 查看配置\n/set_timeout <秒> - 设置超时",
)

my_config = on_command("my_config", priority=10, block=True)

@my_config.handle()
async def show_config(event: MessageEvent):
    can_use, reason = await check_runtime_permission(event.bot, event, config)
    if not can_use:
        await my_config.finish(f"❌ {reason}")

    await my_config.finish(
        f"插件状态: {'启用' if config.plugin_enable else '禁用'}\n"
        f"超时时间: {config.timeout} 秒\n"
        f"用户白名单: {len(config.user_whitelist)} 个"
    )

set_timeout = on_command("set_timeout", priority=10, block=True)

@set_timeout.handle()
async def set_timeout_cmd(event: MessageEvent, args: Message = CommandArg()):
    can_use, reason = await check_runtime_permission(event.bot, event, config)
    if not can_use:
        await set_timeout.finish(f"❌ {reason}")

    try:
        timeout = int(args.extract_plain_text())
        config_manager.update_field("timeout", timeout)
        await set_timeout.finish(f"✅ 超时时间已设置为 {timeout} 秒")
    except ValueError:
        await set_timeout.finish("❌ 请输入有效的数字")
```
