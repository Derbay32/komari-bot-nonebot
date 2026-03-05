"""Komari Chat - 群聊消息处理与 AI 聊天插件。"""

from typing import Any

from nonebot import logger, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.plugin import PluginMetadata, require

from .handlers.message_handler import MessageHandler

# 依赖插件
permission_manager_plugin = require("permission_manager")
require("komari_memory")

from komari_bot.plugins.komari_memory import get_plugin_manager
from komari_bot.plugins.komari_memory.services.config_interface import get_config

__plugin_meta__ = PluginMetadata(
    name="小鞠聊天",
    description="群聊消息流程与 AI 聊天插件（依赖 Komari Memory）",
    usage="自动运行，无需命令",
)

matcher = on_message(priority=10, block=False)

_handler: MessageHandler | None = None


def _resolve_runtime_components(
) -> tuple[Any, Any, Any | None] | None:
    manager = get_plugin_manager()
    if manager is None or manager.redis is None or manager.memory is None:
        return None
    return manager.redis, manager.memory, manager.scene_runtime


def _get_or_build_handler() -> MessageHandler | None:
    global _handler  # noqa: PLW0603

    components = _resolve_runtime_components()
    if components is None:
        return None
    redis, memory, scene_runtime = components

    if (
        _handler is None
        or _handler.redis is not redis
        or _handler.memory is not memory
    ):
        _handler = MessageHandler(
            redis=redis,
            memory=memory,
            scene_runtime=scene_runtime,
        )
    return _handler


@matcher.handle()
async def handle_group_message(bot: Bot, event: GroupMessageEvent) -> None:
    """处理群聊消息。"""
    config = get_config()
    if not config.plugin_enable:
        return

    handler = _get_or_build_handler()
    if handler is None:
        logger.debug("[KomariChat] KomariMemory 未就绪，跳过消息处理")
        return

    group_id = str(event.group_id)
    if group_id not in config.group_whitelist:
        return

    can_use, _ = await permission_manager_plugin.check_runtime_permission(
        bot, event, config
    )
    if not can_use:
        return

    try:
        result = await handler.process_message(event)
        if not result:
            return

        reply = result.get("reply")
        reply_to_message_id = result.get("reply_to_message_id")
        if not reply:
            return

        if reply_to_message_id:
            message_array = [
                {"type": "reply", "data": {"id": reply_to_message_id}},
                {"type": "text", "data": {"text": reply}},
            ]
            try:
                await bot.call_api(
                    "send_group_msg",
                    group_id=int(event.group_id),
                    message=message_array,
                )
            except Exception as e:
                logger.warning("[KomariChat] 原生回复失败: %s，降级普通发送", e)
                await matcher.send(reply)
        else:
            await matcher.send(reply)
    except Exception:
        logger.exception("[KomariChat] 消息处理失败")
