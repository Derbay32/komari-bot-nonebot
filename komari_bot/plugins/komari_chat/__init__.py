"""Komari Chat - 群聊消息处理与 AI 聊天插件。"""

from typing import Any

from nonebot import logger, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.plugin import PluginMetadata, require

from komari_bot.common.onebot_rules import group_message_rule

from .handlers.message_handler import DebugReplyResult, MessageHandler

# 依赖插件
permission_manager_plugin = require("permission_manager")
user_ban_plugin = require("user_ban")
memory_plugin = require("komari_memory")
decision_plugin = require("komari_decision")

get_memory_plugin_manager = memory_plugin.get_plugin_manager
get_decision_plugin_manager = decision_plugin.get_plugin_manager

from komari_bot.plugins.komari_memory.services.config_interface import get_config

__plugin_meta__ = PluginMetadata(
    name="小鞠聊天",
    description="群聊消息流程与 AI 聊天插件（依赖 Komari Memory）",
    usage="自动运行，无需命令",
)

matcher = on_message(rule=group_message_rule(), priority=10, block=False)

_handler: MessageHandler | None = None


def _resolve_runtime_components() -> tuple[Any, Any, Any | None] | None:
    memory_manager = get_memory_plugin_manager()
    if (
        memory_manager is None
        or memory_manager.redis is None
        or memory_manager.memory is None
    ):
        return None
    decision_manager = get_decision_plugin_manager()
    scene_runtime = None if decision_manager is None else decision_manager.scene_runtime
    return memory_manager.redis, memory_manager.memory, scene_runtime


def _get_or_build_handler() -> MessageHandler | None:
    global _handler  # noqa: PLW0603

    components = _resolve_runtime_components()
    if components is None:
        return None
    redis, memory, scene_runtime = components

    if _handler is None or _handler.redis is not redis or _handler.memory is not memory:
        _handler = MessageHandler(
            redis=redis,
            memory=memory,
            scene_runtime=scene_runtime,
        )
    return _handler


async def _send_face_reaction(bot: Bot, event: GroupMessageEvent) -> None:
    """在触发聊天回复后，对触发消息添加表情反应。"""
    config = get_config()
    if not config.face_reaction_enabled or not config.face_reaction_id:
        return

    try:
        await bot.call_api(
            "set_msg_emoji_like",
            message_id=event.message_id,
            emoji_id=config.face_reaction_id,
        )
    except Exception as e:
        logger.debug("[KomariChat] 表情反应发送失败: {}", e)


async def generate_debug_reply(
    *,
    group_id: str,
    user_id: str,
    user_nickname: str,
    content: str,
    bot: Bot | None = None,
    image_urls: list[str] | None = None,
    reply_context: Any = None,
    collector: Any = None,
) -> DebugReplyResult:
    """debug 干跑回复生成：复用 ``_get_or_build_handler()`` 获取 handler，
    调用 ``generate_debug_reply()`` 执行纯读取/生成，不执行任何副作用。

    此 API 不检查聊天插件开关、群白名单或正常权限配置。
    底层依赖未初始化时抛出 RuntimeError。

    Args:
        group_id: 群 ID
        user_id: 命令发起者 ID
        user_nickname: 命令发起者昵称
        content: 测试文本
        bot: Bot 实例（可选）
        image_urls: 命令附图的 URL 列表
        reply_context: 引用消息上下文（ReplyContext），可为 None
        collector: 可选的 LLMDiagnosticCollector，缺省时自行创建

    Returns:
        DebugReplyResult

    Raises:
        RuntimeError: 底层服务（Redis / Memory）未初始化
    """
    handler = _get_or_build_handler()
    if handler is None:
        raise RuntimeError(  # noqa: TRY003
            "KomariChat 底层服务未初始化（Redis 或 Memory 未就绪），无法执行 debug reply。"
        )

    return await handler.generate_debug_reply(
        group_id=group_id,
        user_id=user_id,
        user_nickname=user_nickname,
        content=content,
        _bot=bot,
        image_urls=image_urls,
        reply_context=reply_context,
        collector=collector,
    )


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

    can_use, _ = await permission_manager_plugin.check_runtime_permission(
        bot, event, config
    )
    if not can_use:
        return

    try:
        reply_allowed = not await user_ban_plugin.is_event_banned(bot, event, "chat")
    except user_ban_plugin.BanServiceUnavailableError as error:
        logger.error("[KomariChat] 用户封禁存储不可用，按故障关闭压制回复：{}", error)
        reply_allowed = False

    try:
        result = await handler.process_message(
            bot,
            event,
            on_reply_triggered=lambda: _send_face_reaction(bot, event),
            reply_allowed=reply_allowed,
        )
        if not result:
            return

        reply = result.reply
        reply_to_message_id = result.reply_to_message_id
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
                logger.warning("[KomariChat] 原生回复失败: {}，降级普通发送", e)
                await matcher.send(reply)
        else:
            await matcher.send(reply)
        await handler.commit_delivered_reply(result)
    except Exception:
        logger.exception("[KomariChat] 消息处理失败")
