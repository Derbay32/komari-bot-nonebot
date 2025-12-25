"""
权限管理器。

提供通用的权限检查功能，支持插件开关、用户/群组白名单等。
"""
from typing import Protocol, runtime_checkable, Union

from nonebot.adapters import Bot
from nonebot.permission import SUPERUSER
from nonebot.adapters.onebot.v11 import MessageEvent as Obv11MessageEvent


@runtime_checkable
class PermissionConfig(Protocol):
    """权限配置协议。

    任何实现此协议的配置对象都可以用于权限检查。
    """
    plugin_enable: bool
    user_whitelist: list[str]
    group_whitelist: list[str]


ConfigType = Union[PermissionConfig, object]


class PermissionManager:
    """权限管理器。

    提供：
    - 插件开关检查
    - 用户/群组白名单检查
    - SUPERUSER 权限处理
    - 完整的命令使用权限检查
    """

    def __init__(self, config: ConfigType):
        """初始化权限管理器。

        Args:
            config: 配置对象，需要包含 plugin_enable、user_whitelist、group_whitelist 字段
        """
        self.config = config

    def is_plugin_enabled(self) -> bool:
        """检查插件是否启用。

        Returns:
            插件是否启用
        """
        return bool(getattr(self.config, "plugin_enable", True))

    def is_user_whitelisted(self, user_id: str) -> bool:
        """检查用户是否在白名单中。

        Args:
            user_id: 用户 ID

        Returns:
            用户是否在白名单中
        """
        whitelist = getattr(self.config, "user_whitelist", [])
        # 如果用户白名单为空，则允许所有用户
        if not whitelist:
            return True
        return user_id in whitelist

    def is_group_whitelisted(self, group_id: str) -> bool:
        """检查群组是否在白名单中。

        Args:
            group_id: 群组 ID

        Returns:
            群组是否在白名单中
        """
        whitelist = getattr(self.config, "group_whitelist", [])
        # 如果群组白名单为空，则允许所有群组
        if not whitelist:
            return True
        return group_id in whitelist

    async def can_use_command(
        self,
        bot: Bot,
        event: Obv11MessageEvent,
    ) -> tuple[bool, str]:
        """检查用户是否可以使用命令。

        Args:
            bot: Bot 实例
            event: 事件实例

        Returns:
            tuple[是否可以使用, 拒绝原因]
        """
        # 检查插件是否启用
        if not self.is_plugin_enabled():
            return False, "插件当前已禁用"

        # 检查用户权限
        user_id = event.get_user_id()

        # SUPER 用户绕过所有检查
        if await SUPERUSER(bot, event):
            return True, ""

        # 检查用户白名单
        is_user_whitelisted = self.is_user_whitelisted(user_id)

        # 如果是群聊消息，检查群组白名单
        group_id = getattr(event, "group_id", None)
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


def create_whitelist_rule(config: ConfigType):
    """创建白名单检查规则。

    Args:
        config: 配置对象

    Returns:
        nonebot.rule.Rule 实例
    """
    from nonebot.rule import Rule

    permission_manager = PermissionManager(config)

    async def check_whitelist(bot: Bot, event: Obv11MessageEvent) -> bool:
        """检查白名单规则。

        Args:
            bot: Bot 实例
            event: 事件实例

        Returns:
            是否通过白名单检查
        """
        can_use, _ = await permission_manager.can_use_command(bot, event)
        return can_use

    return Rule(check_whitelist)
