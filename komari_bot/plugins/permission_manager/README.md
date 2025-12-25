# Permission Manager 插件

通用权限管理插件，提供插件开关、白名单检查等权限控制功能。

## 功能特性

- **插件开关检查**：控制插件是否启用
- **白名单管理**：支持用户和群组白名单
- **SUPERUSER 处理**：超级用户绕过所有限制
- **权限装饰器**：便捷的权限检查装饰器
- **Rule 集成**：与 NoneBot 事件处理系统集成

## 安装

将插件放置在 `komari_bot/plugins/permission_manager/` 目录下。

确保已安装依赖：
```bash
pip install nonebot2
```
*真的会有人不安这个吗？*

## 使用方法

### 1. 基本权限检查

```python
from nonebot.plugin import require
from permission_manager import PermissionManager, check_runtime_permission

require("permission_manager")

# 假设你有一个配置对象
config = config_manager.get()

# 创建权限管理器
pm = PermissionManager(config)

# 检查插件是否启用
if pm.is_plugin_enabled():
    logger.info("插件已启用")

# 检查用户是否在白名单中
if pm.is_user_whitelisted("123456"):
    logger.info("用户在白名单中")

# 检查群组是否在白名单中
if pm.is_group_whitelisted("111"):
    logger.info("群组在白名单中")
```

### 2. 在事件处理器中使用

```python
from nonebot import on_command
from nonebot.adapters.onebot.v11 import MessageEvent
from permission_manager import check_runtime_permission

my_command = on_command("my_cmd", priority=10, block=True)

@my_command.handle()
async def handle_my_command(event: MessageEvent):
    # 运行时权限检查
    can_use, reason = await check_runtime_permission(
        event.bot,
        event,
        config
    )

    if not can_use:
        await my_command.finish(f"❌ {reason}")

    # 权限检查通过，执行逻辑
    await my_command.finish("✅ 命令执行成功")
```

### 3. 使用 Rule 集成

```python
from nonebot import on_command
from permission_manager import create_whitelist_rule

# 创建带有白名单检查的 Rule
whitelist_rule = create_whitelist_rule(config)

# 使用 Rule 注册命令
my_command = on_command(
    "my_cmd",
    rule=whitelist_rule,
    priority=10,
    block=True
)

@my_command.handle()
async def handle_my_command(event: MessageEvent):
    # 不需要手动检查权限，Rule 会自动处理
    await my_command.finish("✅ 命令执行成功")
```

### 4. 使用装饰器

```python
from permission_manager import get_permission_checker

# 获取权限检查装饰器
permission_checker = get_permission_checker(config)

@my_command.handle()
@permission_checker  # 应用装饰器
async def handle_my_command(event: MessageEvent):
    # 装饰器会自动检查权限，失败时发送拒绝消息
    await my_command.finish("✅ 命令执行成功")
```

### 5. 获取用户信息

```python
from permission_manager import get_user_nickname

@my_command.handle()
async def handle_my_command(event: MessageEvent):
    # 获取用户昵称（优先群昵称 > 用户昵称 > 用户ID）
    nickname = get_user_nickname(event)
    await my_command.finish(f"你好，{nickname}！")
```

### 6. 格式化权限信息

```python
from permission_manager import format_permission_info, check_plugin_status

@my_command.handle()
async def show_status(event: MessageEvent):
    # 获取插件状态
    is_enabled, status_desc = await check_plugin_status(config)

    # 格式化权限信息
    info = format_permission_info(config)

    await my_command.finish(f"{status_desc}\n{info}")
```

## 配置要求

权限管理器需要一个包含以下字段的配置对象：

```python
class Config:
    plugin_enable: bool      # 插件开关
    user_whitelist: list[str]  # 用户白名单
    group_whitelist: list[str] # 群组白名单
```

推荐继承 `BaseConfigSchema`：

```python
from config_manager import BaseConfigSchema

class MyConfig(BaseConfigSchema):
    # plugin_enable, user_whitelist, group_whitelist 已包含
    api_key: str = ""
```

## API 参考

### PermissionManager

#### `__init__(config: ConfigType)`

初始化权限管理器。

**参数：**
- `config`: 配置对象

#### `is_plugin_enabled() -> bool`

检查插件是否启用。

#### `is_user_whitelisted(user_id: str) -> bool`

检查用户是否在白名单中。

**参数：**
- `user_id`: 用户 ID

**返回：** 如果白名单为空或用户在白名单中返回 `True`

#### `is_group_whitelisted(group_id: str) -> bool`

检查群组是否在白名单中。

**参数：**
- `group_id`: 群组 ID

**返回：** 如果白名单为空或群组在白名单中返回 `True`

#### `async can_use_command(bot: Bot, event: MessageEvent) -> tuple[bool, str]`

检查用户是否可以使用命令。

**参数：**
- `bot`: Bot 实例
- `event`: 事件实例

**返回：** `(是否可以使用, 拒绝原因)`

**权限逻辑：**
1. SUPERUSER 无条件通过
2. 检查插件是否启用
3. 私聊：检查用户白名单
4. 群聊：用户或群组任一在白名单中即可

### 便捷函数

#### `check_runtime_permission(bot, event, config) -> tuple[bool, str]`

使用运行时配置检查权限。

```python
can_use, reason = await check_runtime_permission(bot, event, config)
```

#### `get_user_nickname(event) -> str`

获取用户昵称。

**优先级：** 群昵称 > 用户昵称 > 用户ID

#### `check_plugin_status(config) -> tuple[bool, str]`

检查插件状态。

```python
is_enabled, desc = await check_plugin_status(config)
# (True, "插件已启用") 或 (False, "插件已禁用")
```

#### `format_permission_info(config) -> str`

格式化权限信息。

```python
info = format_permission_info(config)
# 返回类似：
# "插件状态: 🟢 启用
#  用户白名单: 无限制
#  群聊白名单: 3 个群聊"
```

#### `create_whitelist_rule(config) -> Rule`

创建白名单检查 Rule。

```python
from nonebot import on_command
rule = create_whitelist_rule(config)
cmd = on_command("test", rule=rule)
```

### 装饰器

#### `PermissionChecker`

权限检查装饰器类。

```python
from permission_manager import get_permission_checker

checker = get_permission_checker(config)

@handler
@checker  # 应用装饰器
async def my_handler(event: MessageEvent):
    pass
```

## 完整示例

```python
"""my_plugin/__init__.py"""
from nonebot.plugin import PluginMetadata, require
from nonebot import on_command
from nonebot.adapters.onebot.v11 import MessageEvent

from config_manager import get_config_manager, BaseConfigSchema
from permission_manager import (
    PermissionManager,
    check_runtime_permission,
    get_user_nickname,
    format_permission_info,
    create_whitelist_rule,
)

require("permission_manager")

class MyConfig(BaseConfigSchema):
    api_key: str = ""

config_manager = get_config_manager("my_plugin", MyConfig)
config = config_manager.initialize()

__plugin_meta__ = PluginMetadata(
    name="my_plugin",
    description="我的插件",
    usage="/status - 查看状态\n/hello - 打招呼",
)

# 方式1：在处理器中手动检查
status = on_command("status", priority=10, block=True)

@status.handle()
async def show_status(event: MessageEvent):
    can_use, reason = await check_runtime_permission(
        event.bot, event, config
    )
    if not can_use:
        await status.finish(f"❌ {reason}")

    info = format_permission_info(config)
    await status.finish(f"📊 插件状态\n{info}")

# 方式2：使用 Rule 自动检查
whitelist_rule = create_whitelist_rule(config)

hello = on_command("hello", rule=whitelist_rule, priority=10, block=True)

@hello.handle()
async def say_hello(event: MessageEvent):
    # Rule 已自动检查权限
    nickname = get_user_nickname(event)
    await hello.finish(f"👋 你好，{nickname}！")
```

## 权限检查流程

```
用户发送命令
    │
    ▼
┌─────────────────────────┐
│ 是否为 SUPERUSER？      │
└─────────────────────────┘
    │ 是              │ 否
    ▼                 ▼
 通过    ┌─────────────────────────┐
          │ 插件是否启用？          │
          └─────────────────────────┘
              │ 是          │ 否
              ▼             ▼
          通过      ┌─────────────────────────┐
                    │ 检查白名单              │
                    │ (私聊: 用户             │
                    │  群聊: 用户 OR 群组)    │
                    └─────────────────────────┘
                        │ 通过      │ 失败
                        ▼           ▼
                      通过    拒绝访问
```

## 注意事项

1. **SUPERUSER 绕过**：超级用户会绕过所有限制，包括插件开关
2. **空白名单**：白名单为空时表示不限制（允许所有）
3. **群聊逻辑**：群聊中用户或群组任一在白名单中即可通过
4. **配置更新**：权限检查使用运行时配置，修改配置后立即生效
5. **错误消息**：权限被拒绝时会返回友好的中文提示
