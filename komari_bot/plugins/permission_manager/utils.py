"""
权限管理便捷函数。

提供各种便捷函数用于权限检查和信息格式化。
"""

from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import MessageEvent as Obv11MessageEvent

from .manager import ConfigType, PermissionManager


def get_user_nickname(event: Obv11MessageEvent) -> str:
    """获取用户昵称。

    优先使用群昵称，其次使用用户昵称，最后使用用户 ID。

    Args:
        event: 事件实例

    Returns:
        用户昵称
    """
    # 尝试获取群昵称
    if hasattr(event, "sender") and event.sender:
        sender_info = event.sender
        if hasattr(sender_info, "card") and sender_info.card:
            return sender_info.card
        # 尝试获取用户昵称
        if hasattr(sender_info, "nickname") and sender_info.nickname:
            return sender_info.nickname

    # 最后返回用户 ID
    if hasattr(event, "get_user_id"):
        return f"用户（{event.get_user_id()}）"

    return "用户"


async def check_plugin_status(config: ConfigType) -> tuple[bool, str]:
    """检查插件状态。

    Args:
        config: 插件配置

    Returns:
        (插件是否启用, 状态描述)
    """
    permission_manager = PermissionManager(config)
    if permission_manager.is_plugin_enabled():
        return True, "插件已启用"
    return False, "插件已禁用"


def format_permission_info(config: ConfigType) -> str:
    """格式化权限信息。

    Args:
        config: 插件配置

    Returns:
        权限信息字符串
    """
    pm = PermissionManager(config)

    status = "🟢 启用" if pm.is_plugin_enabled() else "🔴 禁用"

    user_whitelist = getattr(pm.config, "user_whitelist", [])
    group_whitelist = getattr(pm.config, "group_whitelist", [])

    user_whitelist_info = (
        "无限制" if not user_whitelist else f"{len(user_whitelist)} 个用户"
    )
    group_whitelist_info = (
        "无限制" if not group_whitelist else f"{len(group_whitelist)} 个群聊"
    )

    return (
        f"插件状态: {status}\n"
        f"用户白名单: {user_whitelist_info}\n"
        f"群聊白名单: {group_whitelist_info}"
    )


def check_context_permission(
    config: ConfigType,
    *,
    user_id: str,
    group_id: str | None,
    is_superuser: bool = False,
) -> tuple[bool, str]:
    """对已认证的用户/群上下文执行与消息事件一致的动态权限检查。"""
    return PermissionManager(config).can_use_context(
        user_id=user_id,
        group_id=group_id,
        is_superuser=is_superuser,
    )


async def check_runtime_permission(
    bot: Bot,
    event: Obv11MessageEvent,
    config: ConfigType,
) -> tuple[bool, str]:
    """使用运行时配置检查权限。

    Args:
        bot: Bot 实例
        event: 事件实例
        config: 配置对象

    Returns:
        (是否可以使用, 拒绝原因)
    """
    permission_manager = PermissionManager(config)
    return await permission_manager.can_use_command(bot, event)
