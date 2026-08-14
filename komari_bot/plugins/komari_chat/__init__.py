"""Komari Chat - 群聊消息处理与 AI 聊天插件。"""

import asyncio
from contextlib import suppress
from types import SimpleNamespace
from typing import Any, cast

from nonebot import get_bots, get_driver, logger, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.plugin import PluginMetadata, require

from komari_bot.onebot.onebot_rules import group_message_rule

from .handlers.message_handler import (
    DebugReplyResult,
    MessageHandler,
    PendingReply,
    ReplyFailureInfo,
)
from .reply_fulfillment_ops_errors import (
    ReplyFulfillmentOpsConflictError,
    ReplyFulfillmentOpsNotFoundError,
    ReplyFulfillmentOpsValidationError,
)
from .services.proactive_reservation import ProactiveReservationService
from .services.reply_delivery_onebot import DeliveryRequest, OneBotReplySender
from .services.reply_fulfillment_ops import (
    ReplyFulfillmentOpsService,
    build_reply_fulfillment_ops_service,
)
from .services.reply_fulfillment_workflow import (
    ReplyFulfillmentWorkflow,
    ReplySender,
    build_reply_fulfillment_workflow,
)

# 依赖插件
require("embedding_provider")
require("permission_manager")
require("user_ban")
require("komari_memory")
require("komari_decision")
require("user_data")

from komari_bot.plugins import komari_memory as memory_plugin
from komari_bot.plugins import permission_manager as permission_manager_plugin
from komari_bot.plugins import user_ban as user_ban_plugin
from komari_bot.plugins import user_data as user_data_plugin

get_memory_plugin_manager = memory_plugin.get_plugin_manager


def _get_reply_fulfillment_config() -> Any:
    """合并聊天配置与记忆配置中履约需要的只读字段。"""
    config = get_config()
    memory_config = get_memory_config()
    return SimpleNamespace(
        proactive_cooldown=config.proactive_cooldown,
        global_interaction_enabled=memory_config.global_interaction_enabled,
        global_interaction_trigger_size=memory_config.global_interaction_trigger_size,
        reply_fulfillment_lease_seconds=config.reply_fulfillment_lease_seconds,
        reply_fulfillment_max_attempts=config.reply_fulfillment_max_attempts,
        reply_fulfillment_retry_base_seconds=config.reply_fulfillment_retry_base_seconds,
        reply_fulfillment_retry_max_seconds=config.reply_fulfillment_retry_max_seconds,
        reply_fulfillment_batch_size=config.reply_fulfillment_batch_size,
        reply_fulfillment_tombstone_retention_days=(
            config.reply_fulfillment_tombstone_retention_days
        ),
        reply_fulfillment_freshness_seconds=(
            config.reply_fulfillment_freshness_seconds
        ),
    )

from komari_bot.plugins.komari_chat.services.config_interface import (
    get_config,
    get_memory_config,
)
from komari_bot.plugins.komari_decision import get_decision_engine

__plugin_meta__ = PluginMetadata(
    name="小鞠聊天",
    description="群聊消息流程与 AI 聊天插件（依赖 Komari Memory）",
    usage="自动运行，无需命令",
)

matcher = on_message(rule=group_message_rule(), priority=10, block=False)

_handler: MessageHandler | None = None
_handler_workflow: ReplyFulfillmentWorkflow | None = None
_reply_fulfillment: ReplyFulfillmentWorkflow | None = None
_reply_fulfillment_components: tuple[Any, Any, Any] | None = None
_reply_fulfillment_ops: ReplyFulfillmentOpsService | None = None
_reply_fulfillment_ops_components: tuple[Any, Any] | None = None
_reply_fulfillment_worker_task: asyncio.Task[None] | None = None


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


def _resolve_ops_components() -> tuple[Any, Any] | None:
    """履约运维的窄运行时边界：只依赖 PostgreSQL 与 Redis。

    判定引擎只服务聊天判定与旧 workflow 的 gating，运维对账不需要它；
    本边界不触碰 ``_resolve_runtime_components`` 的 decision-engine
    gating，避免影响 handler / legacy workflow 既有就绪语义。
    """
    memory_manager = get_memory_plugin_manager()
    if (
        memory_manager is None
        or memory_manager.redis is None
        or memory_manager.memory is None
    ):
        return None
    return memory_manager.redis, memory_manager.memory


def _get_or_build_handler() -> MessageHandler | None:
    global _handler, _handler_workflow  # noqa: PLW0603

    components = _resolve_runtime_components()
    if components is None:
        return None
    redis, memory, decision_engine = components
    if (
        _handler is not None
        and _handler.decision_engine is decision_engine
        and _handler_workflow is _reply_fulfillment
        and _reply_fulfillment is not None
    ):
        return _handler

    workflow = _get_or_build_reply_fulfillment()
    if workflow is None:
        return None

    if (
        _handler is None
        or _handler_workflow is not workflow
        or _handler.decision_engine is not decision_engine
    ):
        _handler = MessageHandler(
            redis=redis,
            memory=memory,
            reply_fulfillment=workflow,
            proactive_reservation=workflow.proactive_reservation,
            decision_engine=decision_engine,
        )
        _handler_workflow = workflow
    return _handler


def _get_recovery_senders() -> dict[tuple[str, str], ReplySender]:
    """按当前在线 Bot 建立恢复 sender 映射（精确身份匹配）。

    恢复只允许冻结时的原 ``bot_self_id`` + ``adapter_name`` 精确匹配
    的在线 Bot 领取；sender 直接使用 ``bot.call_api``，不依赖 matcher
    的隐式事件上下文。
    """
    return {
        (str(bot.self_id), str(bot.type)): cast(
            "ReplySender", OneBotReplySender(bot)
        )
        for bot in get_bots().values()
    }


def _get_or_build_reply_fulfillment() -> ReplyFulfillmentWorkflow | None:
    """构建并缓存回复履约工作流及其私有持久化 adapter。"""
    global _reply_fulfillment, _reply_fulfillment_components  # noqa: PLW0603

    components = _resolve_runtime_components()
    if components is None:
        return None
    redis, memory, _decision_engine = components

    current_components = _reply_fulfillment_components
    same_components = current_components is not None and all(
        current is actual
        for current, actual in zip(current_components, components, strict=True)
    )
    if _reply_fulfillment is None or not same_components:
        redis_client = getattr(redis, "redis", redis)
        proactive_reservation = ProactiveReservationService(redis_client)
        _reply_fulfillment = build_reply_fulfillment_workflow(
            pg_pool=memory.pg_pool,
            redis=redis,
            proactive_reservation=proactive_reservation,
            user_data=user_data_plugin,
            config_getter=_get_reply_fulfillment_config,
            recovery_senders_getter=_get_recovery_senders,
            bots_provider=get_bots,
            superusers_provider=_get_superusers,
        )
        _reply_fulfillment_components = components
    return _reply_fulfillment


def _get_superusers() -> set[str]:
    """当前配置声明的 SUPERUSERS（告警私聊收件人）。"""
    return set(get_driver().config.superusers)


def get_reply_fulfillment_ops_service() -> ReplyFulfillmentOpsService | None:
    """顶层窄 seam：向管理插件提供履约运维服务，不暴露内部 adapter。

    只操作新父子表（不读取旧 outbox、不做双读双写），也不启动或唤醒
    任何 worker；只依赖 PostgreSQL 与 Redis，判定引擎故障不拖垮运维；
    依赖未就绪时返回 None，由管理 API 翻译为 503。
    """
    global _reply_fulfillment_ops, _reply_fulfillment_ops_components  # noqa: PLW0603

    components = _resolve_ops_components()
    if components is None:
        return None
    redis, memory = components

    current_components = _reply_fulfillment_ops_components
    same_components = current_components is not None and all(
        current is actual
        for current, actual in zip(current_components, components, strict=True)
    )
    if _reply_fulfillment_ops is None or not same_components:
        redis_client = getattr(redis, "redis", redis)
        _reply_fulfillment_ops = build_reply_fulfillment_ops_service(
            pg_pool=memory.pg_pool,
            redis_client=redis_client,
        )
        _reply_fulfillment_ops_components = components
    return _reply_fulfillment_ops


async def _reply_fulfillment_worker() -> None:
    """周期恢复中断的回复履约：发送前恢复、承诺推进、告警与小时级清理。"""
    while True:
        try:
            workflow = _get_or_build_reply_fulfillment()
            if workflow is not None:
                await workflow.recover_pending()
            interval = get_config().reply_fulfillment_worker_interval_seconds
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[KomariChat] 回复履约后台轮询失败")
            interval = 5
        await asyncio.sleep(max(1, interval))


async def _start_reply_fulfillment_worker() -> None:
    """启动单进程回复履约轮询任务。"""
    global _reply_fulfillment_worker_task  # noqa: PLW0603
    if _reply_fulfillment_worker_task is None or _reply_fulfillment_worker_task.done():
        _reply_fulfillment_worker_task = asyncio.create_task(
            _reply_fulfillment_worker()
        )


async def _stop_reply_fulfillment_worker() -> None:
    """停止回复履约轮询任务并等待退出。"""
    global _reply_fulfillment_worker_task  # noqa: PLW0603
    task = _reply_fulfillment_worker_task
    _reply_fulfillment_worker_task = None
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


driver = get_driver()
driver.on_startup(_start_reply_fulfillment_worker)
driver.on_shutdown(_stop_reply_fulfillment_worker)


async def _send_face_reaction(bot: Bot, event: GroupMessageEvent) -> None:
    """在开始生成回复时，对触发消息添加表情反应（提示“正在生成”）。"""
    config = get_memory_config()
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
    config = get_memory_config()
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
    try:
        pending_reply = await handler.process_message(
            bot,
            event,
            on_reply_triggered=lambda: _send_face_reaction(bot, event),
            reply_allowed=reply_allowed,
        )
        if pending_reply is None:
            return

        workflow = _get_or_build_reply_fulfillment()
        if workflow is None:
            msg = "KomariChat 回复履约工作流未初始化"
            raise RuntimeError(msg)  # noqa: TRY301

        async def _send_reply(actual_pending_reply: Any) -> object:
            """用 OneBot 窄边界发送，统一富文本/纯文本降级与三态翻译。

            发送载荷投影自履约冻结的群与引用目标，不依赖 matcher 的
            隐式事件上下文；平台异常细节由边界翻译，不在此旁路。
            """
            request = cast(
                "DeliveryRequest",
                SimpleNamespace(
                    group_id=actual_pending_reply.message.group_id,
                    reply=actual_pending_reply.reply,
                    reply_to_message_id=actual_pending_reply.reply_to_message_id,
                ),
            )
            return await OneBotReplySender(bot)(request)

        fulfilled = await workflow.fulfill(
            pending_reply,
            send_reply=_send_reply,
        )
        if fulfilled is False:
            return
        decision_payload = getattr(pending_reply, "decision_payload", None)
        log_decision = getattr(handler, "_log_decision", None)
        if decision_payload is not None and callable(log_decision):
            log_decision(decision_payload)
    except Exception as exc:
        logger.exception("[KomariChat] 消息处理失败")
        # 失败善后：reaction_sent 以 PendingReply 字段为真源（生成前是否贴出表情）；
        # 回复未送达时补发群内错误文本，所有未处理异常均通知 SUPERUSER
        await handler.report_reply_failure(
            bot=bot,
            event=event,
            failure=ReplyFailureInfo(
                stage="deliver" if pending_reply is not None else "process",
                error_type=type(exc).__name__,
                summary=str(exc),
                request_trace_id=(
                    pending_reply.request_trace_id
                    if pending_reply is not None
                    else None
                ),
                reaction_sent=(
                    pending_reply.reaction_sent
                    if pending_reply is not None
                    else False
                ),
            ),
            reason=pending_reply.reason if pending_reply is not None else None,
        )


# 跨插件普通 import 只允许经本顶层暴露面（ADR-0006）：外部插件不得
# import 任意 komari_chat.* 子模块。
__all__ = [
    "ReplyFulfillmentOpsConflictError",
    "ReplyFulfillmentOpsNotFoundError",
    "ReplyFulfillmentOpsValidationError",
    "generate_debug_reply",
    "get_reply_fulfillment_ops_service",
]
