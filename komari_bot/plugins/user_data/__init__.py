from nonebot import logger
from nonebot.plugin import PluginMetadata, require

from .config_schema import DynamicConfigSchema
from .database import UserDataDB
from .models import (
    FavorabilityAdjustmentResult,
    FavorabilityStage,
    UserFavorability,
    get_favorability_stage,
)

__plugin_meta__ = PluginMetadata(
    name="user_data",
    description="当前好感度数据服务插件",
    usage="提供 API 供其他插件调用，管理用户当前好感度数据",
    config=DynamicConfigSchema,
)

config_manager_plugin = require("config_manager")
config_manager = config_manager_plugin.get_config_manager("user_data", DynamicConfigSchema)

_db: UserDataDB | None = None


def get_config() -> DynamicConfigSchema:
    """获取 user_data 动态配置。"""
    return config_manager.get()


async def get_db() -> UserDataDB:
    """获取数据库实例。"""
    global _db  # noqa: PLW0603
    if _db is None:
        logger.debug("[UserData] 首次获取数据库实例，开始初始化")
        db = UserDataDB(get_config())
        await db.initialize()
        _db = db
        logger.debug("[UserData] 数据库实例初始化完成")
    return _db


async def on_startup() -> None:
    """插件启动时的初始化。"""
    config = get_config()
    if not config.plugin_enable:
        logger.info("用户数据插件未启用，跳过初始化")
        return

    try:
        await get_db()
    except Exception as e:
        logger.error(f"用户数据插件数据库初始化失败: {e}")
        return

    logger.info("用户数据插件已启动")


async def on_shutdown() -> None:
    """插件关闭时的清理。"""
    global _db  # noqa: PLW0603
    if _db:
        await _db.close()
        _db = None
        logger.info("用户数据插件已关闭")


async def get_user_favorability(user_id: str) -> UserFavorability:
    """获取用户当前好感度，无记录时创建初始值。"""
    db = await get_db()
    return await db.get_user_favorability(user_id)


async def adjust_user_favorability(
    user_id: str,
    delta: int,
) -> FavorabilityAdjustmentResult:
    """调整用户当前好感度并限制在 [0, 400]。"""
    logger.debug("[UserData] 收到好感度调整请求: user={} delta={}", user_id, delta)
    db = await get_db()
    result = await db.adjust_user_favorability(user_id, delta)
    logger.debug(
        "[UserData] 好感度调整请求完成: user={} before={} delta={} after={}",
        result.user_id,
        result.before,
        result.delta,
        result.after,
    )
    return result


async def get_user_count() -> int:
    """获取总用户数。"""
    db = await get_db()
    return await db.get_user_count()


__all__ = [
    "FavorabilityAdjustmentResult",
    "FavorabilityStage",
    "UserFavorability",
    "adjust_user_favorability",
    "get_favorability_stage",
    "get_user_count",
    "get_user_favorability",
]

__plugin_startup__ = on_startup
__plugin_shutdown__ = on_shutdown
