"""
权限检查装饰器。

提供便捷的装饰器用于权限检查。
"""

from typing import Callable, Awaitable, Any

from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import MessageEvent as Obv11MessageEvent
from nonebot.adapters import MessageTemplate

from .manager import PermissionManager, ConfigType


class PermissionChecker:
    """权限检查器装饰器。

    用于装饰需要权限检查的函数，在函数执行前进行权限验证。
    """

    def __init__(self, config: ConfigType):
        """初始化权限检查器。

        Args:
            config: 配置对象
        """
        self.config = config
        self.permission_manager = PermissionManager(config)

    def __call__(self, func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        """装饰器函数。

        Args:
            func: 被装饰的函数

        Returns:
            包装后的异步函数
        """

        async def wrapper(bot: Bot, event: Obv11MessageEvent, *args: Any, **kwargs: Any) -> Any:
            # 检查权限
            can_use, reason = await self.permission_manager.can_use_command(bot, event)
            if not can_use:
                # 权限检查失败，发送拒绝消息
                await bot.send(event, MessageTemplate("❌ {}").format(reason))
                return

            # 权限检查通过，执行原函数
            return await func(bot, event, *args, **kwargs)

        return wrapper


def get_permission_checker(config: ConfigType) -> PermissionChecker:
    """获取权限检查器实例。

    Args:
        config: 配置对象

    Returns:
        PermissionChecker 实例
    """
    return PermissionChecker(config)
