"""基于 QQ 号的全局用户封禁插件。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nonebot import get_driver, logger
from nonebot.message import run_preprocessor
from nonebot.plugin import PluginMetadata, require

require("nonebot_plugin_apscheduler")

if TYPE_CHECKING:
    from datetime import datetime

from .api import register_user_ban_api
from .event_support import (
    get_event_user_id,
    is_configured_superuser_id,
    is_event_banned,
    is_superuser_id,
)
from .expiration_worker import register_expiration_job, unregister_expiration_job
from .models import (
    BanListPage,
    BanMutationResult,
    BanRecord,
    BanScope,
    BanTargetScope,
    NotificationResult,
    UserBanStatus,
    normalize_ban_reason,
    parse_ban_duration,
)
from .preprocessor import enforce_command_ban
from .service import BanServiceUnavailableError, get_service

__plugin_meta__ = PluginMetadata(
    name="user_ban",
    description="SUPERUSER 用户封禁管理插件，分别控制聊天回复与其他功能",
    usage=(
        ".ban chat|command|all <user_id> [permanent|Nm|Nh|Nd|Nw] [理由...]\n"
        ".unban chat|command|all <user_id>\n"
        ".ban status <user_id>\n"
        ".ban list [chat|command|all] [page]"
    ),
)

run_preprocessor(enforce_command_ban)

driver = get_driver()


@driver.on_startup
async def on_startup() -> None:
    """初始化封禁存储并注册自然解封任务。"""
    register_expiration_job()
    try:
        await get_service().initialize()
    except BanServiceUnavailableError:
        logger.exception("[UserBan] 插件初始化失败，非 SUPERUSER 功能将故障关闭")
        return
    logger.info("[UserBan] 用户封禁插件已启动")


@driver.on_shutdown
async def on_shutdown() -> None:
    """注销自然解封任务并释放数据库资源。"""
    unregister_expiration_job()
    await get_service().close()
    logger.info("[UserBan] 用户封禁插件已关闭")


async def is_user_banned(user_id: str, scope: BanScope) -> bool:
    """检查 QQ 用户是否被封禁指定作用域。"""
    return await get_service().is_user_banned(user_id, scope)


async def ban_user(
    *,
    user_id: str,
    target_scope: BanTargetScope,
    operator_id: str,
    expires_at: datetime | None = None,
    reason: str | None = None,
) -> BanMutationResult:
    """新增或覆盖指定 QQ 用户封禁。"""
    return await get_service().ban_user(
        user_id=user_id,
        target_scope=target_scope,
        operator_id=operator_id,
        expires_at=expires_at,
        reason=reason,
    )


async def unban_user(
    *,
    user_id: str,
    target_scope: BanTargetScope,
) -> BanMutationResult:
    """解封指定 QQ 用户。"""
    return await get_service().unban_user(
        user_id=user_id,
        target_scope=target_scope,
    )


async def get_user_ban_status(user_id: str) -> UserBanStatus:
    """查询指定 QQ 用户的封禁状态。"""
    return await get_service().get_status(user_id)


async def list_user_bans(
    *,
    scope: BanScope | None,
    limit: int,
    offset: int,
) -> BanListPage:
    """分页列出封禁用户。"""
    return await get_service().list_bans(scope=scope, limit=limit, offset=offset)


from . import commands  # noqa: F401

__all__ = [
    "BanListPage",
    "BanMutationResult",
    "BanRecord",
    "BanScope",
    "BanServiceUnavailableError",
    "BanTargetScope",
    "NotificationResult",
    "UserBanStatus",
    "ban_user",
    "get_event_user_id",
    "get_service",
    "get_user_ban_status",
    "is_configured_superuser_id",
    "is_event_banned",
    "is_superuser_id",
    "is_user_banned",
    "list_user_bans",
    "normalize_ban_reason",
    "parse_ban_duration",
    "register_user_ban_api",
    "unban_user",
]
