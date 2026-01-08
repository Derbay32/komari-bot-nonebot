"""角色绑定插件 - 提供跨插件的角色名管理功能。"""

from nonebot import get_driver
from nonebot.plugin import PluginMetadata

# 导入命令模块以注册命令处理器（必须在 manager 之后导入以避免循环导入）
from . import commands  # noqa: F401
from .manager import CharacterBindingManager, get_manager

__plugin_meta__ = PluginMetadata(
    name="character_binding",
    description="提供跨插件的角色名绑定管理功能",
    usage="/bind set <角色名> - 设置绑定\n"
    "/bind del - 删除绑定\n"
    "/bind list - 查看绑定列表",
)


driver = get_driver()


@driver.on_startup
async def init_plugin() -> None:
    """插件启动时初始化管理器。"""
    # 触发单例初始化，确保启动时加载数据并输出日志
    get_manager()


def get_binding_manager() -> CharacterBindingManager:
    """获取角色绑定管理器实例。

    Returns:
        管理器实例
    """
    return get_manager()


def get_character_name(
    user_id: str,
    fallback_nickname: str | None = None,
) -> str:
    """便捷函数：获取用户的角色名。

    Args:
        user_id: 用户ID
        fallback_nickname: 备用昵称

    Returns:
        角色名称
    """
    return get_manager().get_character_name(user_id, fallback_nickname)


__all__ = [
    "CharacterBindingManager",
    "get_binding_manager",
    "get_character_name",
]
