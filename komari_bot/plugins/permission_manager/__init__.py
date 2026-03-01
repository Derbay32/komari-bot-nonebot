"""
权限管理插件 - 通用权限检查功能。

提供：
- 插件开关检查
- 用户/群组白名单检查
- SUPERUSER 权限处理
- 权限检查装饰器
- 便捷函数

使用示例：
```python
from nonebot.plugin import require
from permission_manager import (
    PermissionManager,
    check_runtime_permission,
    get_user_nickname,
)

# 获取配置
config = config_manager.get()

# 创建权限管理器
pm = PermissionManager(config)

# 检查权限
can_use, reason = await pm.can_use_command(bot, event)

# 或使用便捷函数
can_use, reason = await check_runtime_permission(bot, event, config)

# 获取用户昵称
nickname = get_user_nickname(event)
```
"""

from nonebot.plugin import PluginMetadata

from .checker import PermissionChecker, get_permission_checker
from .manager import PermissionConfig, PermissionManager, create_whitelist_rule
from .utils import (
    check_plugin_status,
    check_runtime_permission,
    format_permission_info,
    get_user_nickname,
)

__plugin_meta__ = PluginMetadata(
    name="permission_manager",
    description="通用权限管理插件，提供插件开关、白名单检查等功能",
    usage="详见插件文档",
)

__all__ = [
    "PermissionChecker",
    "PermissionConfig",
    "PermissionManager",
    "check_plugin_status",
    "check_runtime_permission",
    "create_whitelist_rule",
    "format_permission_info",
    "get_permission_checker",
    "get_user_nickname",
]
