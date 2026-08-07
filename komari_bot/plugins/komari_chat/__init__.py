"""Komari Chat - 群聊消息处理与 AI 聊天插件。"""

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast

from nonebot import get_driver, logger, on_message
from nonebot.adapters.onebot.v11 import ActionFailed, Bot, GroupMessageEvent
from nonebot.plugin import PluginMetadata, require

from komari_bot.onebot.onebot_messages import plain_text_message
from komari_bot.onebot.onebot_rules import group_message_rule

from .handlers.message_handler import (
    DebugReplyResult,
    MessageHandler,
    PendingReply,
    ReplyFailureInfo,
)
from .services.error_notify import one_line_summary

if TYPE_CHECKING:
    from collections.abc import Awaitable

# 依赖插件
permission_manager_plugin = require("permission_manager")
user_ban_plugin = require("user_ban")
memory_plugin = require("komari_memory")
require("komari_decision")

get_memory_plugin_manager = memory_plugin.get_plugin_manager

from komari_bot.plugins.komari_decision import get_decision_engine
from komari_bot.plugins.komari_memory.services.config_interface import get_config

__plugin_meta__ = PluginMetadata(
    name="小鞠聊天",
    description="群聊消息流程与 AI 聊天插件（依赖 Komari Memory）",
    usage="自动运行，无需命令",
)

matcher = on_message(rule=group_message_rule(), priority=10, block=False)

_handler: MessageHandler | None = None
_reply_commit_worker_task: asyncio.Task[None] | None = None


def _resolve_runtime_components() -> tuple[Any, Any, Any] | None:
    memory_manager = get_memory_plugin_manager()
    if (
        memory_manager is None
        or memory_manager.redis is None
        or memory_manager.memory is None
    ):
        return None
    decision_engine = get_decision_engine()
    if decision_engine is None:
        return None
    return memory_manager.redis, memory_manager.memory, decision_engine


def _get_or_build_handler() -> MessageHandler | None:
    global _handler  # noqa: PLW0603

    components = _resolve_runtime_components()
    if components is None:
        return None
    redis, memory, decision_engine = components

    if _handler is None or _handler.decision_engine is not decision_engine:
        _handler = MessageHandler(
            redis=redis,
            memory=memory,
            decision_engine=decision_engine,
        )
    return _handler


async def _reply_commit_worker() -> None:
    """周期重试已经确认送达的聊天副作用 outbox。"""
    while True:
        try:
            handler = _get_or_build_handler()
            if handler is not None:
                await handler.retry_pending_reply_commits()
            interval = get_config().reply_commit_worker_interval_seconds
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[KomariChat] 回复 outbox 后台轮询失败")
            interval = 5
        await asyncio.sleep(max(1, interval))


async def _start_reply_commit_worker() -> None:
    """启动单进程 outbox 轮询任务。"""
    global _reply_commit_worker_task  # noqa: PLW0603
    if _reply_commit_worker_task is None or _reply_commit_worker_task.done():
        _reply_commit_worker_task = asyncio.create_task(_reply_commit_worker())


async def _stop_reply_commit_worker() -> None:
    """停止 outbox 轮询任务并等待退出。"""
    global _reply_commit_worker_task  # noqa: PLW0603
    task = _reply_commit_worker_task
    _reply_commit_worker_task = None
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


driver = get_driver()
driver.on_startup(_start_reply_commit_worker)
driver.on_shutdown(_stop_reply_commit_worker)


async def _send_face_reaction(bot: Bot, event: GroupMessageEvent) -> None:
    """在开始生成回复时，对触发消息添加表情反应（提示“正在生成”）。"""
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


def _extract_platform_message_id(response: object) -> str | None:
    """从 OneBot/NoneBot 发送结果中提取可对账的平台消息 ID。"""
    candidate: object | None = None
    if isinstance(response, dict):
        candidate = response.get("message_id")
        if candidate is None and isinstance(response.get("data"), dict):
            candidate = response["data"].get("message_id")
    else:
        candidate = getattr(response, "message_id", None)
    if candidate is None:
        return None
    value = str(candidate).strip()
    return value or None


async def generate_debug_reply(
    *,
    group_id: str,
    user_id: str,
    user_nickname: str,
    content: str,
    bot: Bot | None = None,
    image_urls: list[str] | None = None,
    reply_context: Any = None,
    caller_is_superuser: bool = False,
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
        caller_is_superuser=caller_is_superuser,
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

    pending_reply: PendingReply | None = None
    reply_delivered = False
    reply_prepared = False
    delivery_outcome = "not_sent"
    platform_message_id: str | None = None
    try:
        pending_reply = await handler.process_message(
            bot,
            event,
            on_reply_triggered=lambda: _send_face_reaction(bot, event),
            reply_allowed=reply_allowed,
        )
        if pending_reply is None:
            return

        reply = pending_reply.reply
        reply_to_message_id = pending_reply.reply_to_message_id
        if not reply:
            await handler.discard_pending_reply(pending_reply)
            return

        prepare_pending = getattr(handler, "prepare_pending_reply", None)
        if callable(prepare_pending):
            reply_prepared = bool(
                await cast(
                    "Awaitable[object]",
                    prepare_pending(pending_reply),
                )
            )
            if not reply_prepared:
                logger.info("[KomariChat] 重复回复 operation 已存在，取消本次发送")
                await handler.discard_pending_reply(pending_reply)
                pending_reply = None
                return

        if reply_to_message_id:
            message_array = [
                {"type": "reply", "data": {"id": reply_to_message_id}},
                {"type": "text", "data": {"text": reply}},
            ]
            try:
                response = await bot.call_api(
                    "send_group_msg",
                    group_id=int(event.group_id),
                    message=message_array,
                )
            except ActionFailed as e:
                logger.warning("[KomariChat] 原生回复失败: {}，降级普通发送", e)
                try:
                    response = await matcher.send(plain_text_message(reply))
                except ActionFailed:
                    raise
                except Exception:
                    delivery_outcome = "unknown"
                    raise
            except Exception:
                delivery_outcome = "unknown"
                raise
            platform_message_id = _extract_platform_message_id(response)
        else:
            try:
                response = await matcher.send(plain_text_message(reply))
            except ActionFailed:
                raise
            except Exception:
                delivery_outcome = "unknown"
                raise
            platform_message_id = _extract_platform_message_id(response)
        delivery_outcome = "delivered"
        reply_delivered = True
        await handler.commit_delivered_reply(
            pending_reply,
            platform_message_id=platform_message_id,
        )
    except Exception as exc:
        if pending_reply is not None and not reply_delivered:
            if delivery_outcome == "not_sent":
                if reply_prepared:
                    cancel_prepared = getattr(handler, "cancel_prepared_reply", None)
                    if callable(cancel_prepared):
                        try:
                            await cast(
                                "Awaitable[object]",
                                cancel_prepared(pending_reply),
                            )
                        except Exception:
                            logger.exception(
                                "[KomariChat] 发送失败后的 outbox 取消失败"
                            )
                await handler.discard_pending_reply(pending_reply)
            else:
                logger.error(
                    "[KomariChat] 平台发送结果未知，保留 PREPARED 记录待对账: operation={}",
                    pending_reply.operation_id,
                )
        logger.exception("[KomariChat] 消息处理失败")
        # 失败善后：pending_reply 存在说明表情已在生成前贴出；
        # 回复未送达时补发群内错误文本，所有未处理异常均通知 SUPERUSER
        await handler.report_reply_failure(
            bot=bot,
            event=event,
            failure=ReplyFailureInfo(
                stage="deliver" if pending_reply is not None else "process",
                error_type=type(exc).__name__,
                summary=one_line_summary(exc),
                request_trace_id=(
                    pending_reply.request_trace_id
                    if pending_reply is not None
                    else None
                ),
                reaction_sent=pending_reply is not None and not reply_delivered,
            ),
            reason=pending_reply.reason if pending_reply is not None else None,
        )
