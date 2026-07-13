"""用户事件封禁判断辅助。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nonebot import get_driver

from .models import BanScope, normalize_qq_user_id
from .service import get_service

if TYPE_CHECKING:
    from nonebot.adapters import Bot, Event


def get_event_user_id(event: Event) -> str | None:
    """尽力从消息或通知事件中提取可靠 QQ 号。"""
    try:
        user_id = normalize_qq_user_id(event.get_user_id())
    except (AttributeError, NotImplementedError, TypeError, ValueError):
        user_id = None
    if user_id is not None:
        return user_id
    return normalize_qq_user_id(getattr(event, "user_id", None))


def is_superuser_id(bot: Bot, user_id: str) -> bool:
    """按 NoneBot 运行时配置判断 SUPERUSER。"""
    return user_id in bot.config.superusers


def is_configured_superuser_id(user_id: str) -> bool:
    """按 NoneBot 全局运行时配置判断 SUPERUSER。"""
    return user_id in get_driver().config.superusers


async def is_event_banned(bot: Bot, event: Event, scope: BanScope) -> bool:
    """检查用户事件是否受指定作用域封禁。"""
    user_id = get_event_user_id(event)
    if user_id is None or is_superuser_id(bot, user_id):
        return False
    return await get_service().is_user_banned(user_id, scope)
