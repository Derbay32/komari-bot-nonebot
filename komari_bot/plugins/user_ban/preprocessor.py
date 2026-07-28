"""非聊天 matcher 的全局封禁预处理器。"""

from __future__ import annotations

# NoneBot 会在运行时解析预处理器注解，必须保留真实类型导入。
from nonebot import logger
from nonebot.adapters import Bot, Event  # noqa: TC002
from nonebot.matcher import Matcher  # noqa: TC002

from .event_support import is_event_banned
from .service import BanServiceUnavailableError

CHAT_PLUGIN_NAME = "komari_chat"


async def enforce_command_ban(matcher: Matcher, bot: Bot, event: Event) -> None:
    """静默清空被 command 封禁用户命中的非聊天 matcher。"""
    if matcher.plugin_name == CHAT_PLUGIN_NAME:
        return

    try:
        blocked = await is_event_banned(bot, event, "command")
    except BanServiceUnavailableError as error:
        logger.error("[UserBan] 封禁存储不可用，按故障关闭拦截 matcher：{}", error)
        blocked = True

    if blocked:
        matcher.remain_handlers.clear()
