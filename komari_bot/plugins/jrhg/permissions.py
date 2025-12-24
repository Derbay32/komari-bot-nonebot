from typing import Union

from nonebot.adapters import Bot
from nonebot.permission import SUPERUSER
from nonebot.rule import Rule
from nonebot.adapters.onebot.v11 import MessageEvent as obv11MessEvent

from .config import Config
from .config_schemas import DynamicConfigSchema

# 配置兼容
ConfigType = Union[Config, DynamicConfigSchema]


class PermissionManager:
    """权限管理器"""

    def __init__(self, config: ConfigType):
        self.config = config

    def is_plugin_enabled(self) -> bool:
        """检查插件是否启用"""
        return self.config.jrhg_plugin_enable

    def is_user_whitelisted(self, user_id: str) -> bool:
        """检查用户是否在白名单中"""
        # 如果用户白名单为空，则允许所有用户
        if not self.config.user_whitelist:
            return True
        return user_id in self.config.user_whitelist

    def is_group_whitelisted(self, group_id: str) -> bool:
        """检查群组是否在白名单中"""
        # 如果群组白名单为空，则允许所有群组
        if not self.config.group_whitelist:
            return True
        return group_id in self.config.group_whitelist

    async def can_use_command(
            self,
            bot: Bot,
            event: obv11MessEvent
            ) -> tuple[bool, str]:
        """检查用户是否可以使用命令

        Args:
            bot: Bot实例
            event: 事件实例

        Returns:
            tuple[是否可以使用, 拒绝原因]
        """
        # 检查插件是否启用
        if not self.is_plugin_enabled():
            return False, "插件当前已禁用"

        # 检查用户权限
        user_id = event.get_user_id()

        # SUPER用户绕过所有检查
        if await SUPERUSER(bot, event):
            return True, ""
        
        # 检查用户白名单
        is_user_whitelisted = self.is_user_whitelisted(user_id)

        # 如果是群聊消息，检查群组白名单
        group_id = getattr(event, 'group_id', None)
        is_group_whitelisted = True
        if group_id is not None:
            is_group_whitelisted = self.is_group_whitelisted(str(group_id))
            # 群聊：用户或群组任一在白名单中即可
            if not (is_user_whitelisted or is_group_whitelisted):
                return False, "用户和群组均不在白名单中，无法使用此命令"
        else:
            # 私聊：只检查用户白名单
            if not is_user_whitelisted:
                return False, "您不在用户白名单中，无法使用此命令"

        return True, ""


def create_whitelist_rule(config: ConfigType) -> Rule:
    """创建白名单检查规则"""
    permission_manager = PermissionManager(config)

    async def check_whitelist(bot: Bot, event: obv11MessEvent) -> bool:
        """检查白名单规则"""
        can_use, _ = await permission_manager.can_use_command(bot, event)
        return can_use

    return Rule(check_whitelist)


def get_user_nickname(event: obv11MessEvent) -> str:
    """获取用户昵称

    Args:
        event: 事件实例

    Returns:
        用户昵称，优先使用群昵称，否则使用用户名
    """
    # 尝试获取群昵称
    if hasattr(event, 'sender') and event.sender:
        sender_info = event.sender
        if hasattr(event.sender, 'card') and sender_info.card:
            return sender_info.card
        # 尝试获取用户昵称
        if hasattr(sender_info, 'nickname') and sender_info.nickname:
            return sender_info.nickname

    # 最后返回用户ID
    if hasattr(event, 'get_user_id'):
        return "用户（{:.0f}）".format(event.get_user_id())

    return "用户"


class PermissionChecker:
    """权限检查器装饰器"""

    def __init__(self, config: ConfigType):
        self.config = config
        self.permission_manager = PermissionManager(config)

    def __call__(self, func):
        """装饰器函数"""
        async def wrapper(bot: Bot, event: obv11MessEvent, *args, **kwargs):
            # 检查权限
            can_use, reason = await self.permission_manager.can_use_command(bot, event)
            if not can_use:
                # 如果权限检查失败，需要通知用户
                from nonebot.adapters import MessageTemplate
                await bot.send(event, MessageTemplate("❌ {}").format(reason))
                return

            # 权限检查通过，执行原函数
            return await func(bot, event, *args, **kwargs)
        return wrapper


def get_permission_checker(config: ConfigType) -> PermissionChecker:
    """获取权限检查器实例"""
    return PermissionChecker(config)


# 便捷函数
async def check_plugin_status(config: ConfigType) -> tuple[bool, str]:
    """检查插件状态

    Args:
        config: 插件配置

    Returns:
        (插件是否启用, 状态描述)
    """
    permission_manager = PermissionManager(config)
    if permission_manager.is_plugin_enabled():
        return True, "插件已启用"
    else:
        return False, "插件已禁用"


def format_permission_info(config: ConfigType) -> str:
    """格式化权限信息

    Args:
        config: 插件配置

    Returns:
        权限信息字符串
    """
    pm = PermissionManager(config)

    status = "🟢 启用" if pm.is_plugin_enabled() else "🔴 禁用"

    user_whitelist_info = "无限制" if not pm.config.user_whitelist else f"{len(pm.config.user_whitelist)} 个用户"
    group_whitelist_info = "无限制" if not pm.config.group_whitelist else f"{len(pm.config.group_whitelist)} 个群聊"

    return (
        f"插件状态: {status}\n"
        f"用户白名单: {user_whitelist_info}\n"
        f"群聊白名单: {group_whitelist_info}"
    )


async def check_runtime_permission(
    bot: Bot,
    event: obv11MessEvent,
    config_manager,
) -> tuple[bool, str]:
    """使用运行时配置检查权限

    Args:
        bot: Bot实例
        event: 事件实例
        config_manager: ConfigManager 实例

    Returns:
        (是否可以使用, 拒绝原因)
    """
    dynamic_config = config_manager.get()
    permission_manager = PermissionManager(dynamic_config)
    return await permission_manager.can_use_command(bot, event)