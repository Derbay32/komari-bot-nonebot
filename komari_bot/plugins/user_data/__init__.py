from nonebot import get_plugin_config
from nonebot.plugin import PluginMetadata
from typing import Optional

from .config import Config
from .models import UserAttribute, UserFavorability, FavorGenerationResult
from .database import UserDataDB

__plugin_meta__ = PluginMetadata(
    name="user_data",
    description="通用用户数据管理插件，提供用户属性存储和好感度管理功能",
    usage="提供API供其他插件调用，管理用户数据",
    config=Config,
)

# 全局数据库实例
_db: Optional[UserDataDB] = None
config: Config = get_plugin_config(Config)


async def get_db() -> UserDataDB:
    """获取数据库实例"""
    global _db
    if _db is None:
        _db = UserDataDB(config.db_path)
        await _db.initialize()
    return _db


# ===== 插件生命周期管理 =====

async def on_startup():
    """插件启动时的初始化"""
    db = await get_db()
    # 可以在这里添加启动时的初始化逻辑
    print("用户数据插件已启动")


async def on_shutdown():
    """插件关闭时的清理"""
    global _db
    if _db:
        await _db.close()
        _db = None
        print("用户数据插件已关闭")


# ===== 公开API接口 =====

async def get_user_favorability(user_id: str, group_id: str) -> Optional[UserFavorability]:
    """获取用户好感度

    Args:
        user_id: 用户ID
        group_id: 群组ID

    Returns:
        用户好感度对象，如果不存在则返回None
    """
    db = await get_db()
    return await db.get_user_favorability(user_id, group_id)


async def generate_or_update_favorability(user_id: str, group_id: str) -> FavorGenerationResult:
    """生成或更新用户好感度

    Args:
        user_id: 用户ID
        group_id: 群组ID

    Returns:
        好感度生成结果，包含每日好感度、累计好感度和态度等级
    """
    db = await get_db()
    return await db.generate_or_update_favorability(user_id, group_id)


async def get_user_attribute(user_id: str, group_id: str, attribute_name: str) -> Optional[str]:
    """获取用户属性

    Args:
        user_id: 用户ID
        group_id: 群组ID
        attribute_name: 属性名称

    Returns:
        属性值，如果不存在则返回None
    """
    db = await get_db()
    return await db.get_user_attribute(user_id, group_id, attribute_name)


async def set_user_attribute(user_id: str, group_id: str, attribute_name: str, attribute_value: str) -> bool:
    """设置用户属性

    Args:
        user_id: 用户ID
        group_id: 群组ID
        attribute_name: 属性名称
        attribute_value: 属性值

    Returns:
        操作是否成功
    """
    db = await get_db()
    return await db.set_user_attribute(user_id, group_id, attribute_name, attribute_value)


async def get_user_attributes(user_id: str, group_id: str) -> list[UserAttribute]:
    """获取用户的所有属性

    Args:
        user_id: 用户ID
        group_id: 群组ID

    Returns:
        用户属性列表
    """
    db = await get_db()
    return await db.get_user_attributes(user_id, group_id)


async def get_favor_history(user_id: str, group_id: str, days: int = 7) -> list[UserFavorability]:
    """获取用户好感度历史记录

    Args:
        user_id: 用户ID
        group_id: 群组ID
        days: 获取最近多少天的记录

    Returns:
        好感度历史记录列表
    """
    db = await get_db()
    return await db.get_favor_history(user_id, group_id, days)


async def cleanup_old_data(retention_days: int = 30) -> bool:
    """清理旧的用户属性数据

    Args:
        retention_days: 数据保留天数，0表示不清理

    Returns:
        操作是否成功
    """
    db = await get_db()
    return await db.cleanup_old_data(retention_days)


async def get_user_count() -> int:
    """获取总用户数

    Returns:
        总用户数
    """
    db = await get_db()
    return await db.get_user_count()


async def get_group_count() -> int:
    """获取总群聊数

    Returns:
        总群聊数
    """
    db = await get_db()
    return await db.get_group_count()


# ===== 便捷函数 =====

async def get_favor_attitude(daily_favor: int) -> str:
    """根据每日好感度获取态度描述

    Args:
        daily_favor: 每日好感度值 (1-100)

    Returns:
        态度描述字符串
    """
    if daily_favor <= 20:
        return "非常冷淡"
    elif daily_favor <= 40:
        return "冷淡"
    elif daily_favor <= 60:
        return "中性"
    elif daily_favor <= 80:
        return "友好"
    else:
        return "非常友好"


async def format_favor_response(ai_response: str, user_nickname: str, daily_favor: int) -> str:
    """格式化好感度回复

    Args:
        ai_response: AI生成的回复内容
        user_nickname: 用户昵称
        daily_favor: 每日好感度值

    Returns:
        格式化后的回复字符串
    """
    return f"{ai_response}\n【小鞠对{user_nickname}今日的好感为{daily_favor}】"


# 导出的主要API
__all__ = [
    # 核心功能API
    "get_user_favorability",
    "generate_or_update_favorability",
    "get_user_attribute",
    "set_user_attribute",
    "get_user_attributes",

    # 历史和统计API
    "get_favor_history",
    "get_user_count",
    "get_group_count",

    # 便捷函数
    "get_favor_attitude",
    "format_favor_response",

    # 数据管理
    "cleanup_old_data",
]

# 注册插件生命周期钩子
__plugin_startup__ = on_startup
__plugin_shutdown__ = on_shutdown
