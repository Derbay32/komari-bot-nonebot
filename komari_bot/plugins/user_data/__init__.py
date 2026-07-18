from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal

from nonebot import get_driver, logger
from nonebot.plugin import PluginMetadata, require

from .config_schema import DynamicConfigSchema
from .database import UserDataDB
from .models import (
    FavorabilityAdjustmentResult,
    FavorabilitySetResult,
    FavorabilityStage,
    UserFavorability,
    get_favorability_stage,
)

if TYPE_CHECKING:
    from nonebot.internal.driver import Driver


class UserDataDisabledError(RuntimeError):
    """user_data 已被动态配置关闭。"""


class UserDataStoppingError(RuntimeError):
    """user_data 正在或已经关闭，不允许重新建立连接池。"""

__plugin_meta__ = PluginMetadata(
    name="user_data",
    description="当前好感度数据服务插件",
    usage="提供 API 供其他插件调用，管理用户当前好感度数据",
    config=DynamicConfigSchema,
)

config_manager_plugin = require("config_manager")
config_manager = config_manager_plugin.get_config_manager("user_data", DynamicConfigSchema)

_db: UserDataDB | None = None
_db_init_lock: asyncio.Lock | None = None
_db_init_lock_loop: asyncio.AbstractEventLoop | None = None
_lifecycle_state: Literal[
    "new",
    "starting",
    "running",
    "stopping",
    "stopped",
] = "new"


def _get_db_init_lock() -> asyncio.Lock:
    """获取绑定到当前事件循环的数据库初始化锁。"""
    global _db_init_lock, _db_init_lock_loop  # noqa: PLW0603
    loop = asyncio.get_running_loop()
    if _db_init_lock is None or _db_init_lock_loop is not loop:
        _db_init_lock = asyncio.Lock()
        _db_init_lock_loop = loop
    return _db_init_lock


def get_config() -> DynamicConfigSchema:
    """获取 user_data 动态配置。"""
    return config_manager.get()


async def _get_config_async() -> DynamicConfigSchema:
    """在事件循环内异步获取 user_data 动态配置。"""
    return await config_manager.get_async()


async def get_db() -> UserDataDB:
    """获取数据库实例。"""
    global _db, _lifecycle_state  # noqa: PLW0603
    config = await _get_config_async()
    if not config.plugin_enable:
        msg = "user_data 插件已禁用"
        raise UserDataDisabledError(msg)
    if _lifecycle_state in {"stopping", "stopped"}:
        msg = "user_data 正在或已经关闭"
        raise UserDataStoppingError(msg)

    if _db is None:
        async with _get_db_init_lock():
            if _lifecycle_state in {"stopping", "stopped"}:
                msg = "user_data 正在或已经关闭"
                raise UserDataStoppingError(msg)
            config = await _get_config_async()
            if not config.plugin_enable:
                msg = "user_data 插件已禁用"
                raise UserDataDisabledError(msg)
            if _db is None:
                logger.debug("[UserData] 首次获取数据库实例，开始初始化")
                previous_state = _lifecycle_state
                _lifecycle_state = "starting"
                db = UserDataDB(config)
                try:
                    await db.initialize()
                except BaseException:
                    if _lifecycle_state not in {"stopping", "stopped"}:
                        _lifecycle_state = (
                            "new" if previous_state == "new" else "running"
                        )
                    raise
                if _lifecycle_state in {"stopping", "stopped"}:
                    await db.close()
                    msg = "user_data 初始化期间进入关闭状态"
                    raise UserDataStoppingError(msg)
                _db = db
                _lifecycle_state = "running"
                logger.debug("[UserData] 数据库实例初始化完成")
    return _db


async def on_startup() -> None:
    """插件启动时的初始化。"""
    global _lifecycle_state  # noqa: PLW0603
    if _lifecycle_state == "stopped":
        msg = "user_data 已完成 shutdown，不能在同一生命周期内重启"
        raise UserDataStoppingError(msg)
    _lifecycle_state = "starting"
    config = await _get_config_async()
    if not config.plugin_enable:
        _lifecycle_state = "running"
        logger.info("[UserData] 插件未启用，跳过初始化")
        return

    try:
        await get_db()
    except Exception as error:
        _lifecycle_state = "running"
        logger.error(
            "[UserData] 数据库初始化失败: error_type={}",
            type(error).__name__,
        )
        return

    _lifecycle_state = "running"
    logger.info("[UserData] 插件已启动")


async def on_shutdown() -> None:
    """插件关闭时的清理。"""
    global _db, _lifecycle_state  # noqa: PLW0603
    _lifecycle_state = "stopping"
    async with _get_db_init_lock():
        db = _db
        _db = None
        try:
            if db is not None:
                await db.close()
                logger.info("[UserData] 插件已关闭")
        finally:
            _lifecycle_state = "stopped"


def _register_lifecycle(driver: Driver) -> None:
    """把 user_data 生命周期接入 NoneBot Driver。"""
    driver.on_startup(on_startup)
    driver.on_shutdown(on_shutdown)


async def get_user_favorability(user_id: str) -> UserFavorability:
    """获取用户当前好感度，无记录时创建初始值。"""
    db = await get_db()
    return await db.get_user_favorability(user_id)


async def adjust_user_favorability(
    user_id: str,
    delta: int,
    *,
    operation_id: str | None = None,
) -> FavorabilityAdjustmentResult:
    """调整用户当前好感度并限制在 [0, 400]。"""
    logger.debug("[UserData] 收到好感度调整请求: user={} delta={}", user_id, delta)
    db = await get_db()
    result = await db.adjust_user_favorability(
        user_id,
        delta,
        operation_id=operation_id,
    )
    logger.debug(
        "[UserData] 好感度调整请求完成: user={} before={} delta={} after={}",
        result.user_id,
        result.before,
        result.delta,
        result.after,
    )
    return result


async def cleanup_favorability_operations(*, retention_days: int) -> int:
    """清理超过聊天防重窗口的好感度幂等账本。"""
    db = await get_db()
    return await db.cleanup_adjustment_ledger(retention_days=retention_days)


async def get_user_count() -> int:
    """获取总用户数。"""
    db = await get_db()
    return await db.get_user_count()


async def set_user_favorability(
    user_id: str,
    value: int,
) -> FavorabilitySetResult:
    """设置用户当前好感度为绝对值（0-400）。

    对新用户以 initial_favorability 作为 before；通过行锁与 adjust 串行化。
    """
    logger.debug("[UserData] 收到好感度设置请求: user={} value={}", user_id, value)
    db = await get_db()
    result = await db.set_user_favorability(user_id, value)
    logger.debug(
        "[UserData] 好感度设置请求完成: user={} before={} after={}",
        result.user_id,
        result.before,
        result.after,
    )
    return result


__all__ = [
    "FavorabilityAdjustmentResult",
    "FavorabilitySetResult",
    "FavorabilityStage",
    "UserDataDisabledError",
    "UserDataStoppingError",
    "UserFavorability",
    "adjust_user_favorability",
    "cleanup_favorability_operations",
    "get_favorability_stage",
    "get_user_count",
    "get_user_favorability",
    "set_user_favorability",
]

try:
    _driver = get_driver()
except ValueError:
    _driver = None

if _driver is not None:
    _register_lifecycle(_driver)
