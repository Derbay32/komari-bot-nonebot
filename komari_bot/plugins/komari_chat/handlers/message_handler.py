"""Komari Memory 消息处理核心。"""

from __future__ import annotations

import asyncio
import json
import re
import time
import traceback
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast, runtime_checkable

from nonebot import logger
from nonebot.adapters.onebot.v11.event import Reply
from nonebot.compat import type_validate_python
from nonebot.exception import FinishedException
from nonebot.permission import SUPERUSER
from nonebot.plugin import require

from komari_bot.decision import (
    DecisionEngineProtocol,
    DecisionOutcome,
    DecisionRuntimeStatus,
)
from komari_bot.onebot import (
    GroupTaskFailureNotification,
    GroupTaskFailureNotifier,
    ImageFailureDiagnostic,
    RedisFailureNotificationCooldown,
    image_failure_reason_code,
)
from komari_bot.plugins.komari_memory import MessageSchema, RedisManager
from komari_bot.plugins.llm_provider.config_schema import DynamicConfigSchema

from ..reply_fulfillment_domain import build_reply_fulfillment_id
from ..services.agent_budget import AgentExecutionBudget
from ..services.config_interface import get_config, get_memory_config
from ..services.image_downloader import (
    download_images_as_base64_aligned,
    extract_image_sources,
)
from ..services.image_reading_session import (
    ImageFailureSummary,
    ImageReadingSession,
    ImageUnderstandingFailureError,
)
from ..services.image_understanding import ImageUnderstandingPolicy
from ..services.llm_service import (
    FETCH_PAGE_TOOL,
    READ_IMAGE_TOOL,
    READ_PROFILE_TOOL,
    RECORD_FAVORABILITY_DELTA_TOOL,
    SEARCH_WEB_TOOL,
    InteractionHistoryRecord,
    NativeMultimodalRequestError,
    ReplyResult,
    generate_reply,
    generate_reply_with_tools,
)
from ..services.proactive_reservation import ReservationDenied, ReservationLostError
from ..services.prompt_builder import build_prompt
from ..services.query_rewrite_service import QueryRewriteService
from ..services.reply_context import ReplyContext

require("agent_run_logger")
require("user_data")

require("config_manager")
require("komari_search")

from komari_bot.plugins import agent_run_logger as agent_run_logger_plugin
from komari_bot.plugins import config_manager as config_manager_plugin
from komari_bot.plugins import komari_search as komari_search_plugin
from komari_bot.plugins import user_data as user_data_plugin

llm_provider_config_manager = config_manager_plugin.get_config_manager(
    "llm_provider",
    DynamicConfigSchema,
)

if TYPE_CHECKING:
    from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent

    from komari_bot.plugins.agent_run_logger.diagnostic import LLMDiagnosticCollector
    from komari_bot.plugins.komari_memory import MemoryService

    from ..services.proactive_reservation import (
        ProactiveLease,
        ProactiveReservationService,
        ReservationHandoff,
    )
    from ..services.reply_fulfillment_workflow import ReplyFulfillmentQueryProtocol

AttemptReplyReason = Literal["at", "direct_call", "score"]
ReplyAction = Literal[
    "replied",
    "replied_forced",
    "not_replied",
    "generation_failed",
    "blocked_by_user_ban",
    "decision_unavailable",
]
ReplyTriggeredCallback = Callable[[], Coroutine[Any, Any, None]]


@dataclass(frozen=True)
class ResolvedReplyContext:
    """引用消息解析结果。"""

    context: ReplyContext | None
    refetched: bool = False


@runtime_checkable
class _PlainTextExtractable(Protocol):
    def extract_plain_text(self) -> str: ...


@dataclass(frozen=True)
class DebugReplyResult:
    """debug 回复干跑结果。"""

    reply: str
    reply_to_message_id: str | None
    favorability_delta: int | None
    favorability_reason: str | None
    interaction_history: InteractionHistoryRecord | None
    collector: LLMDiagnosticCollector


@dataclass(frozen=True)
class PendingReply:
    """已生成但尚未确认送达的回复及其待提交副作用。

    bot_self_id 与 adapter_name 由 OneBot 边界的 bot.self_id 与
    适配器身份冻结，随回复一起成为履约不可变身份的一部分。
    """

    reply: str
    reply_to_message_id: str
    message: MessageSchema
    reply_result: ReplyResult
    force_reply: bool
    bot_nickname: str
    bot_self_id: str
    adapter_name: str
    reason: AttemptReplyReason
    reply_score: float | None
    fulfillment_id: str
    request_trace_id: str
    reply_timestamp: float
    proactive_reservation_id: str | None = None
    proactive_handoff: ReservationHandoff | None = None
    reaction_sent: bool = False
    decision_payload: dict[str, object] | None = None


GROUP_ERROR_TEXT = "啊、啊呜……对不起，脑袋里刚才突然乱成一团了……"


@dataclass(frozen=True)
class ReplyFailureInfo:
    """一次回复尝试失败的极简诊断信息（不含消息正文 / prompt / 回复正文）。

    reaction_sent 是失败分流边界标志：True 表示已向用户消息贴出“生成中”表情、
    用户正处于等待回复状态，失败时需要补发群内错误文本。

    image_failure_summary（TSK-196）：图片理解失败时携带的安全聚合摘要
    （``ImageFailureSummary``，无 URL/base64/正文）；只在消息处理器的通知
    边界经本地窄 mapper 投影为 onebot ``ImageFailureDiagnostic``，存在时
    ``report_reply_failure`` 只提交一张图片汇总卡。
    """

    stage: str
    error_type: str
    summary: str | None
    request_trace_id: str | None
    reaction_sent: bool
    image_failure_summary: ImageFailureSummary | None = None


def _to_image_diagnostic(
    summary: ImageFailureSummary | None,
) -> ImageFailureDiagnostic | None:
    """把安全的 ``ImageFailureSummary`` 投影为 onebot 窄诊断（仅白名单字段）。

    TSK-196 深模块边界：chat 领域只传递 ``ImageFailureSummary``，只有在
    消息处理器的通知边界才经本地窄 mapper 构造 onebot
    ``ImageFailureDiagnostic``；``ImageFailureDiagnostic`` 构造时运行时校验
    并确定性去重排序，恶意值（URL/base64/CQ/换行）会 ValueError。
    """
    if summary is None:
        return None
    return ImageFailureDiagnostic(
        mode=summary.mode,
        failed_count=summary.failed_images,
        stages=summary.stages,
        error_types=summary.error_types,
    )


def _native_failure_summary(
    *,
    total_images: int,
    download_failures: int,
    provider_failed: bool,
    all_unavailable: bool,
) -> ImageFailureSummary:
    """构造 native 模式图片失败安全摘要（无 URL/base64/正文）。

    TSK-196：native 批量下载失败与带图主 LLM 失败统一投影为
    ``ImageFailureSummary``，供消息处理器经共享通知边界提交图片汇总卡。
    provider 整体失败时，进入有效范围的全部图片都未被成功理解：失败数量
    恒为 ``total_images``（即使部分图片下载成功、部分下载失败），
    error_types/stages 同时保留 download+vision。
    """
    failed = total_images if provider_failed else download_failures
    error_types: set[str] = set()
    stages: set[str] = set()
    if download_failures:
        error_types.add("image_unavailable")
        stages.add("download")
    if provider_failed:
        error_types.add("vision_failed")
        stages.add("vision")
    return ImageFailureSummary(
        mode="native",
        all_images_unavailable=all_unavailable,
        total_images=total_images,
        attempted_images=total_images,
        failed_images=failed,
        error_types=tuple(sorted(error_types)),
        stages=tuple(sorted(stages)),
    )


class _FavorabilityReadError(RuntimeError):
    """读取当前好感度失败。"""


class MessageHandler:
    """消息处理核心。"""

    def __init__(
        self,
        redis: RedisManager,
        memory: MemoryService,
        reply_fulfillment: ReplyFulfillmentQueryProtocol,
        proactive_reservation: ProactiveReservationService,
        decision_engine: DecisionEngineProtocol,
    ) -> None:
        """初始化消息处理器。"""
        self.redis = redis
        self.memory = memory
        self.reply_fulfillment = reply_fulfillment
        self.query_rewrite = QueryRewriteService()
        self._reaction_tasks: set[asyncio.Task[None]] = set()
        self.decision_engine = decision_engine
        self.proactive_reservation = proactive_reservation

    def _is_at_trigger(self, event: GroupMessageEvent) -> bool:
        """检查是否 @ 了机器人。"""
        return bool(hasattr(event, "to_me") and event.to_me)

    @staticmethod
    def _is_reply_to_bot(event: GroupMessageEvent) -> bool:
        """检查当前消息是否引用了机器人消息。"""
        reply = event.reply
        if reply is None or reply.sender.user_id is None:
            return False
        return str(reply.sender.user_id) == str(event.self_id)

    @staticmethod
    def _strip_text_at_alias_prefix(
        message_content: str,
        aliases: list[str],
    ) -> str | None:
        """剥离纯文本形式的 `@机器人别名` 前缀。"""
        cleaned_aliases = sorted(
            {alias.strip() for alias in aliases if alias and alias.strip()},
            key=len,
            reverse=True,
        )
        if not cleaned_aliases:
            return None

        alias_pattern = "|".join(re.escape(alias) for alias in cleaned_aliases)
        match = re.match(
            rf"^\s*(?:@|\uFF20)\s*(?:{alias_pattern})(?:[\s,，。.!！?？:：、~-]|\uFF5E)*",
            message_content,
            flags=re.IGNORECASE,
        )
        if not match:
            return None

        stripped_content = message_content[match.end() :].lstrip()
        return stripped_content or message_content

    def _resolve_trigger_message(
        self,
        event: GroupMessageEvent,
    ) -> tuple[bool, str]:
        """解析当前消息是否应按 `@机器人` 直通处理，并返回清洗后的文本。"""
        message_content = event.get_plaintext()
        if self._is_at_trigger(event) or self._is_reply_to_bot(event):
            return True, message_content

        config = get_memory_config()
        stripped_content = self._strip_text_at_alias_prefix(
            message_content,
            [config.bot_nickname, *config.bot_aliases],
        )
        if stripped_content is None:
            return False, message_content

        logger.debug(
            "[KomariChat] 纯文本 @ 命中机器人别名，按 at_trigger 处理: raw={} cleaned={}",
            message_content,
            stripped_content,
        )
        return True, stripped_content

    @staticmethod
    def _safe_round(value: float | None) -> float | None:
        if value is None:
            return None
        return round(value, 4)

    @staticmethod
    def _reply_fulfillment_id(message: MessageSchema) -> str:
        """由平台事件稳定生成聊天回复履约 ID（复用领域构建函数）。"""
        return build_reply_fulfillment_id(
            group_id=message.group_id,
            trigger_message_id=message.message_id,
            trigger_user_id=message.user_id,
        )

    @staticmethod
    def _extract_plain_text_from_message(message: object) -> str:
        if isinstance(message, _PlainTextExtractable):
            return str(message.extract_plain_text()).strip()

        if isinstance(message, str):
            return message.strip()

        if isinstance(message, list):
            return "".join(
                str(seg.get("data", {}).get("text", ""))
                for seg in message
                if isinstance(seg, dict) and str(seg.get("type", "")) == "text"
            ).strip()

        return ""

    @staticmethod
    def _build_reply_context(
        *,
        reply: Reply,
        bot_self_id: str,
    ) -> ReplyContext | None:
        text = MessageHandler._extract_plain_text_from_message(reply.message)
        image_sources, image_count = extract_image_sources(reply.message)

        if not text and image_count <= 0:
            return None

        user_id = (
            str(reply.sender.user_id) if reply.sender.user_id is not None else None
        )
        user_nickname = (
            str(reply.sender.card or reply.sender.nickname).strip()
            if (reply.sender.card or reply.sender.nickname)
            else user_id
        )

        return ReplyContext(
            source_side="assistant" if user_id == bot_self_id else "user",
            message_id=str(reply.message_id),
            user_id=user_id,
            user_nickname=user_nickname,
            text=text,
            image_sources=tuple(image_sources),
            image_count=image_count,
            has_visible_image=bool(image_sources),
        )

    @staticmethod
    def _should_refetch_reply_context(
        *,
        context: ReplyContext | None,
    ) -> bool:
        return context is None or (
            context.image_count > 0 and not context.has_visible_image
        )

    @staticmethod
    async def _refetch_reply(
        *,
        bot: Bot,
        reply: Reply,
    ) -> Reply | None:
        try:
            payload = await bot.get_msg(message_id=int(reply.message_id))
            return type_validate_python(Reply, payload)
        except Exception:
            logger.debug(
                "[KomariMemory] 补取引用消息失败: message_id={}",
                reply.message_id,
                exc_info=True,
            )
            return None

    @staticmethod
    async def _refetch_reply_context_by_message_id(
        *,
        bot: Bot,
        message_id: str,
    ) -> ReplyContext | None:
        """按消息 ID 补取引用消息并构造上下文。"""
        try:
            payload = await bot.get_msg(message_id=int(message_id))
            reply = type_validate_python(Reply, payload)
        except Exception:
            logger.debug(
                "[KomariMemory] 补取 debug 引用消息失败: message_id={}",
                message_id,
                exc_info=True,
            )
            return None

        return MessageHandler._build_reply_context(
            reply=reply,
            bot_self_id=str(bot.self_id),
        )

    async def _resolve_reply_context(
        self,
        *,
        bot: Bot,
        event: GroupMessageEvent,
        at_trigger: bool,
    ) -> ResolvedReplyContext:
        if not at_trigger or event.reply is None:
            return ResolvedReplyContext(context=None, refetched=False)

        context = self._build_reply_context(
            reply=event.reply,
            bot_self_id=str(event.self_id),
        )
        if not self._should_refetch_reply_context(context=context):
            return ResolvedReplyContext(context=context, refetched=False)

        refetched_reply = await self._refetch_reply(bot=bot, reply=event.reply)
        if refetched_reply is None:
            return ResolvedReplyContext(context=context, refetched=True)

        refetched_context = self._build_reply_context(
            reply=refetched_reply,
            bot_self_id=str(event.self_id),
        )
        return ResolvedReplyContext(
            context=refetched_context or context,
            refetched=True,
        )

    def _build_decision_payload(
        self,
        *,
        group_id: str,
        user_id: str,
        message_id: str,
        outcome: DecisionOutcome,
        reply_action: ReplyAction,
    ) -> dict[str, object]:
        return {
            "group_id": group_id,
            "user_id": user_id,
            "message_id": message_id,
            "alias_hit": outcome.alias_hit,
            "call_intent": outcome.call_intent,
            "call_margin": self._safe_round(outcome.call_margin),
            "memory_action": outcome.memory_action,
            "reply_action": reply_action,
            "forced_reply_reason": outcome.forced_reply_reason,
            "filter_reason": outcome.filter_reason,
            "reply_score": self._safe_round(outcome.reply_score),
            "timing_score": self._safe_round(outcome.timing_score),
            "scene_score": self._safe_round(outcome.scene_score),
            "best_scene_id": outcome.best_scene_id,
            "noise_score": self._safe_round(outcome.noise_score),
            "meaningful_score": self._safe_round(outcome.meaningful_score),
            "call_direct_score": self._safe_round(outcome.call_direct_score),
            "call_mention_score": self._safe_round(outcome.call_mention_score),
            "decision_runtime_status": outcome.runtime_status.value,
            "decision_runtime_reason": outcome.runtime_reason,
        }

    def _log_decision(self, payload: dict[str, object]) -> None:
        """输出决策日志（info 摘要 + debug 完整结构）。"""
        logger.info(
            "[KomariMemory] decision_summary group={} user={} msg={} "
            "memory={} reply={} reason={} intent={} scene={} "
            "reply_score={} timing={}",
            payload.get("group_id"),
            payload.get("user_id"),
            payload.get("message_id"),
            payload.get("memory_action"),
            payload.get("reply_action"),
            payload.get("forced_reply_reason"),
            payload.get("call_intent"),
            payload.get("best_scene_id"),
            payload.get("reply_score"),
            payload.get("timing_score"),
        )
        logger.debug(
            "[KomariMemory] decision_full={}",
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )

    async def process_message(
        self,
        bot: Bot,
        event: GroupMessageEvent,
        on_reply_triggered: ReplyTriggeredCallback | None = None,
        *,
        reply_allowed: bool = True,
    ) -> PendingReply | None:
        """处理群聊消息的主流程。"""
        user_id = str(event.user_id)
        group_id = str(event.group_id)
        at_trigger, message_content = self._resolve_trigger_message(event)
        message_id = str(event.message_id)
        reply_context_result = await self._resolve_reply_context(
            bot=bot,
            event=event,
            at_trigger=at_trigger,
        )

        image_urls, image_count = extract_image_sources(event.message)
        if image_count:
            logger.info("[KomariMemory] 检测到 {} 张图片", image_count)

        user_nickname = (
            (event.sender.nickname or event.sender.card or user_id)
            if event.sender
            else user_id
        )
        message = MessageSchema(
            user_id=user_id,
            user_nickname=user_nickname,
            group_id=group_id,
            content=message_content,
            timestamp=time.time(),
            message_id=message_id,
        )

        outcome = await self.decision_engine.evaluate(
            message_content=message_content,
            group_id=group_id,
            at_trigger=at_trigger,
        )
        memory_store = outcome.memory_action == "store"

        if outcome.filter_reason is not None:
            logger.debug(
                "[KomariMemory] 消息被过滤: {} - {}...",
                outcome.filter_reason,
                message_content[:30],
            )
            await self._handle_low_value(message)
            self._log_decision(
                self._build_decision_payload(
                    group_id=group_id,
                    user_id=user_id,
                    message_id=message_id,
                    outcome=outcome,
                    reply_action="not_replied",
                )
            )
            return None

        if not outcome.should_reply:
            if memory_store:
                await self._handle_normal_message(message)
            else:
                await self._handle_low_value(message)
            self._log_decision(
                self._build_decision_payload(
                    group_id=group_id,
                    user_id=user_id,
                    message_id=message_id,
                    outcome=outcome,
                    reply_action=(
                        "not_replied"
                        if outcome.runtime_status is DecisionRuntimeStatus.READY
                        else "decision_unavailable"
                    ),
                )
            )
            return None

        if not reply_allowed:
            if memory_store:
                await self._handle_normal_message(message)
            else:
                await self._handle_low_value(message)
            self._log_decision(
                self._build_decision_payload(
                    group_id=group_id,
                    user_id=user_id,
                    message_id=message_id,
                    outcome=outcome,
                    reply_action="blocked_by_user_ban",
                )
            )
            return None

        fulfillment_id = self._reply_fulfillment_id(message)
        if await self.reply_fulfillment.is_duplicate_event(fulfillment_id):
            logger.info(
                "[KomariChat] 重复平台事件已有回复 fulfillment，跳过生成: group={} message={}",
                group_id,
                message_id,
            )
            return None

        reason: AttemptReplyReason = (
            outcome.reply_reason if outcome.reply_reason != "none" else "score"
        )
        pending_reply, stored, failure = await self._attempt_reply(
            message=message,
            reply_to_message_id=message_id,
            image_urls=image_urls,
            reply_context=reply_context_result.context,
            reply_context_requested=at_trigger and event.reply is not None,
            reply_context_refetched=reply_context_result.refetched,
            force_reply=outcome.force_reply,
            reason=reason,
            reply_score=outcome.reply_score,
            store_current=memory_store,
            caller_is_superuser=await SUPERUSER(bot, event),
            on_reply_triggered=on_reply_triggered,
            bot_self_id=str(bot.self_id),
            adapter_name=bot.type,
        )
        if pending_reply is not None:
            # TSK-196：成功任务带图片失败摘要 → 最多提交一次 SUPERUSER 图片
            # 汇总卡（``group_text=None`` 无群消息），与失败路径共用共享通知
            # 边界与冷却；debug 干跑走 ``generate_debug_reply`` 不经过这里。
            if pending_reply.reply_result.image_failure_summary is not None:
                await self._notify_image_failure_summary(
                    bot=bot,
                    event=event,
                    summary=pending_reply.reply_result.image_failure_summary,
                    request_trace_id=pending_reply.request_trace_id,
                )
            reply_action: ReplyAction = (
                "replied_forced" if outcome.force_reply else "replied"
            )
            return replace(
                pending_reply,
                decision_payload=self._build_decision_payload(
                    group_id=group_id,
                    user_id=user_id,
                    message_id=message_id,
                    outcome=outcome,
                    reply_action=reply_action,
                ),
            )

        if memory_store and not stored:
            await self._handle_normal_message(message)

        self._log_decision(
            self._build_decision_payload(
                group_id=group_id,
                user_id=user_id,
                message_id=message_id,
                outcome=outcome,
                reply_action="generation_failed",
            )
        )
        if failure is not None:
            await self.report_reply_failure(
                bot=bot,
                event=event,
                failure=failure,
                reason=reason,
            )
        return None

    async def _handle_low_value(self, message: MessageSchema) -> None:
        """处理低价值消息（直接丢弃，不存储）。"""
        logger.debug("[KomariMemory] 低价值消息已丢弃: {}...", message.content[:30])

    async def _handle_normal_message(self, message: MessageSchema) -> None:
        """处理普通消息（连续追加到当前会话缓冲区）。"""
        await self.redis.push_message(message.group_id, message)

    def _schedule_reply_reaction(
        self,
        callback: ReplyTriggeredCallback | None,
    ) -> bool:
        """在生成回复前 fire-and-forget 派发表情反应；返回是否已派发。

        表情发送失败维持静默 DEBUG 日志语义，不阻塞生成。
        """
        config = get_memory_config()
        if (
            callback is None
            or not config.face_reaction_enabled
            or not config.face_reaction_id
        ):
            return False
        task = asyncio.create_task(callback())
        self._reaction_tasks.add(task)
        task.add_done_callback(self._consume_reaction_task)
        return True

    def _consume_reaction_task(self, task: asyncio.Task[None]) -> None:
        """回收表情任务引用并兜底记录未被回调吞掉的异常。"""
        self._reaction_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            logger.debug("[KomariChat] 表情反应任务执行失败", exc_info=True)

    async def report_reply_failure(
        self,
        *,
        bot: Bot,
        event: GroupMessageEvent,
        failure: ReplyFailureInfo,
        reason: str | None,
    ) -> None:
        """回复失败善后：贴过表情则补发群内错误文本，并通知 SUPERUSER。

        普通投递故障由共享通知边界隔离记录，不在此抛出；
        任务取消（CancelledError）继续向上传播，不吞没。
        """
        try:
            notify_superusers = get_memory_config().error_notify_enabled
        except Exception:
            logger.exception(
                "[KomariChat] 回复失败通知配置读取失败，静默 SUPERUSER 私聊"
            )
            notify_superusers = False
        logger.debug(
            "[KomariChat] 回复失败善后: reason={} error_type={} image={}",
            reason,
            failure.error_type,
            failure.image_failure_summary is not None,
        )
        # TSK-196 复审：图片 summary→diagnostic 映射单独隔离。映射意外失败时
        # fail-closed 静默 SU 图片卡（绝不把不合法 summary 降级为可能泄漏的
        # generic 私聊摘要），但仍经共享 notifier 发送 group_text（reaction_sent
        # 分流），不吞掉既有群内固定道歉。
        diagnostic: ImageFailureDiagnostic | None = None
        mapping_failed = False
        if failure.image_failure_summary is not None:
            try:
                diagnostic = _to_image_diagnostic(failure.image_failure_summary)
            except Exception:
                mapping_failed = True
                # 不捕获 exc_info：本函数作用域可能持有 image_failure_summary，
                # 避免任何含 URL/base64 的帧被写入日志（TSK-196 复审）。
                logger.error("[KomariChat] 图片失败摘要→诊断映射失败，静默 SUPERUSER 私聊")
        try:
            notifier = GroupTaskFailureNotifier(
                cooldown=RedisFailureNotificationCooldown(
                    cast("Any", self.redis.redis)
                ),
            )
            if mapping_failed:
                # fail-closed：notify_superusers=False + summary=None + 无诊断，
                # 只投递 group_text（若 reaction_sent），不降级为 generic 摘要。
                reason_code = failure.error_type
                notify_superusers = False
                summary = None
            else:
                # TSK-196：图片理解失败只经共享通知边界提交一次 SUPERUSER 汇总卡
                # （``group_text`` 仍按 reaction_sent 分流）；同一群+同图片
                # reason_code 跨任务共享 Redis 冷却，存储不可用故障开放。
                reason_code = (
                    image_failure_reason_code(diagnostic)
                    if diagnostic is not None
                    else failure.error_type
                )
                summary = None if diagnostic is not None else failure.summary
            await notifier.notify(
                bot=bot,
                notification=GroupTaskFailureNotification(
                    group_id=int(event.group_id),
                    message_id=int(event.message_id),
                    group_text=GROUP_ERROR_TEXT if failure.reaction_sent else None,
                    task_kind="chat_reply",
                    stage=failure.stage,
                    reason_code=reason_code,
                    notify_superusers=notify_superusers,
                    request_trace_id=failure.request_trace_id,
                    summary=summary,
                    image_diagnostic=diagnostic,
                ),
            )
        except Exception:
            # 不捕获 exc_info：本函数作用域可能持有 image_failure_summary，
            # 避免任何含 URL/base64 的帧被写入日志（TSK-196 复审）。
            logger.error("[KomariChat] 回复失败善后上报异常")

    async def _notify_image_failure_summary(
        self,
        *,
        bot: Bot,
        event: GroupMessageEvent,
        summary: ImageFailureSummary,
        request_trace_id: str,
    ) -> None:
        """成功任务带图片失败摘要时，最多提交一次 SUPERUSER 图片汇总卡。

        只在消息处理器的通知边界把领域 ``ImageFailureSummary`` 映射为 onebot
        窄诊断（TSK-196 深模块边界）；不向群内发送任何消息
        （``group_text=None``）；``notify/cooldown/投递/映射`` 异常一律吞掉，
        不影响主流程；debug 干跑路径绝不调用本方法。
        """
        try:
            diagnostic = _to_image_diagnostic(summary)
            if diagnostic is None:
                return
            notify_superusers = get_memory_config().error_notify_enabled
        except Exception:
            logger.error(
                "[KomariChat] 图片失败汇总配置/映射失败，静默 SUPERUSER 私聊"
            )
            return
        try:
            notifier = GroupTaskFailureNotifier(
                cooldown=RedisFailureNotificationCooldown(
                    cast("Any", self.redis.redis)
                ),
            )
            await notifier.notify(
                bot=bot,
                notification=GroupTaskFailureNotification(
                    group_id=int(event.group_id),
                    message_id=int(event.message_id),
                    group_text=None,
                    task_kind="chat_reply",
                    stage="generate",
                    reason_code=image_failure_reason_code(diagnostic),
                    notify_superusers=notify_superusers,
                    request_trace_id=request_trace_id,
                    summary=None,
                    image_diagnostic=diagnostic,
                ),
            )
        except Exception:
            logger.error("[KomariChat] 图片失败汇总上报异常")

    @staticmethod
    def _select_recent_context(
        messages: list[MessageSchema],
        *,
        max_messages: int,
        max_utf8_bytes: int,
        max_estimated_tokens: int,
    ) -> list[MessageSchema]:
        """从最新消息向前选择一段连续且满足请求级预算的上下文。"""
        selected_reversed: list[MessageSchema] = []
        used_bytes = 0
        used_tokens = 0
        for message in reversed(messages[-max_messages:]):
            budget_text = (
                f"{message.user_id}\n{message.user_nickname}\n{message.content}\n"
            )
            encoded = budget_text.encode("utf-8", errors="replace")
            message_bytes = len(encoded)
            message_tokens = max(
                (len(budget_text) + 3) // 4,
                (message_bytes + 2) // 3,
            )
            if (
                used_bytes + message_bytes > max_utf8_bytes
                or used_tokens + message_tokens > max_estimated_tokens
            ):
                break
            selected_reversed.append(message)
            used_bytes += message_bytes
            used_tokens += message_tokens
        selected_reversed.reverse()
        return selected_reversed

    async def _read_buffers(
        self,
        *,
        group_id: str,
        user_id: str,
        message: MessageSchema,
        store_current: bool,
    ) -> tuple[list[MessageSchema], list[dict[str, object]], bool]:
        """读取已有缓冲：recent messages + global interaction buffer。

        Returns:
            (recent_messages, interaction_records, stored)
        """
        config = get_memory_config()
        stored = False

        context_messages_limit = int(getattr(config, "context_messages_limit", 10))
        recent_messages = await self.redis.get_buffer(
            group_id,
            limit=context_messages_limit,
        )
        recent_messages = self._select_recent_context(
            recent_messages,
            max_messages=context_messages_limit,
            max_utf8_bytes=int(getattr(config, "context_max_utf8_bytes", 24_000)),
            max_estimated_tokens=int(
                getattr(config, "context_max_estimated_tokens", 6_000)
            ),
        )
        try:
            interaction_records = await self.redis.get_global_interaction_buffer(
                user_id,
                limit=10,
            )
        except Exception:
            logger.debug(
                "[KomariChat] 近期互动原始缓冲读取失败，跳过注入: user={}",
                user_id,
                exc_info=True,
            )
            interaction_records = []

        if store_current:
            await self._handle_normal_message(message)
            stored = True

        return recent_messages, interaction_records, stored

    async def _generate_reply_core(
        self,
        *,
        message: MessageSchema,
        recent_messages: list[MessageSchema],
        interaction_records: list[dict[str, object]],
        image_urls: list[str] | None,
        reply_context: ReplyContext | None,
        reply_context_requested: bool,
        reply_context_refetched: bool,
        request_trace_id: str,
        caller_is_superuser: bool = False,
        collector: LLMDiagnosticCollector | None = None,
    ) -> ReplyResult:
        """纯读取/生成核心：查询重写、记忆/画像/好感度读取、prompt 构建、LLM 回复生成。

        不执行任何副作用：不写 Redis、不调好感度、不写互动历史、不设冷却。
        """
        config = get_memory_config()
        # TSK-192：任务起点从 komari_chat 配置冻结回复 Agent 执行预算，
        # 普通 / debug / 简单三条入口共享同一份冻结快照；
        # 任务中配置变更不影响当前任务，只作用于下一个任务。
        # TSK-194 / ADR-0010：图片理解模式与下载预算同为任务级冻结值——
        # 普通 / debug 入口都在此处读取一次；原生模式图片直接交给聊天模型
        # 多模态输入，委托模式暴露 read_image 工具。
        chat_config = get_config()
        agent_budget = AgentExecutionBudget.from_config(chat_config)
        image_policy = ImageUnderstandingPolicy.from_config(chat_config)

        # 查询重写（带 trace）
        if collector is not None:
            rewrite_parent_call_id = f"rewrite-{uuid.uuid4().hex[:8]}"
        else:
            rewrite_parent_call_id = None

        rewritten_query = await self.query_rewrite.rewrite_query(
            current_query=message.content,
            request_trace_id=request_trace_id,
            parent_call_id=rewrite_parent_call_id,
            collector=collector,
        )

        try:
            from komari_bot.plugins import embedding_provider

            query_embedding = await embedding_provider.embed(rewritten_query)
        except Exception as e:
            logger.warning("[KomariMemory] 预生成查询特征向量失败: {}", e)
            query_embedding = None

        memories = await self.memory.search_conversations(
            query=rewritten_query,
            group_id=message.group_id,
            user_id=message.user_id,
            limit=config.memory_search_limit,
            query_embedding=query_embedding,
        )
        try:
            interaction_memories = await self.memory.search_interaction_events(
                user_id=message.user_id,
                query=rewritten_query,
                limit=config.memory_search_limit,
                query_embedding=query_embedding,
            )
        except Exception:
            logger.debug(
                "[KomariChat] 长期互动事件记忆检索失败，跳过注入: user={}",
                message.user_id,
                exc_info=True,
            )
            interaction_memories = []

        try:
            current_user_profile = await self.memory.get_user_profile(
                user_id=message.user_id,
                group_id=message.group_id,
            )
        except Exception:
            logger.debug(
                "[KomariChat] 当前用户画像读取失败，跳过注入: user={}",
                message.user_id,
                exc_info=True,
            )
            current_user_profile = None

        reply_sources = list(reply_context.image_sources) if reply_context else []
        current_sources = image_urls or []
        combined_sources = [*reply_sources, *current_sources]

        reply_image_urls: list[str] | None = None
        base64_image_urls: list[str] | None = None
        image_session: ImageReadingSession | None = None
        # 委托模式才向回复 Agent 暴露 read_image 工具；原生模式图片作为
        # 多模态输入直接嵌入 (user) 消息，由聊天模型原生理解。
        use_vision_tool = False
        # native 批量下载失败计数与有效图片数（仅 native 分支使用，初始化以
        # 覆盖未进入分支的场景）。
        native_download_failures = 0
        effective_total = 0
        # TSK-196 复审：所有预期图片失败（native 全下载失败 / native provider
        # 失败 / delegated 全部不可用）统一汇聚到本函数末尾的单点安全抛出点；
        # 状态与成功结果提前初始化，覆盖未进入分支/异常提前退出场景。
        _image_failure_summary: ImageFailureSummary | None = None
        reply_result: ReplyResult | None = None
        if image_policy.is_delegated:
            # TSK-195 / ADR-0010：delegated 任务起点零预下载；引用消息图片
            # 在前、当前消息图片在后，索引在会话内稳定固定；只有首次
            # read_image(image_index) 才经安全下载器懒下载该索引并交给视觉
            # 子调用。构造会话不触达网络，视觉槽位参数在任务起点冻结一次。
            if combined_sources:
                vision_config = cast(
                    "DynamicConfigSchema", llm_provider_config_manager.get()
                )
                image_session = ImageReadingSession.build(
                    quoted_sources=reply_sources,
                    current_sources=current_sources,
                    policy=image_policy.download,
                    vision_model=vision_config.vision_model,
                    vision_temperature=vision_config.vision_temperature,
                    vision_max_tokens=vision_config.vision_max_tokens,
                    vision_request_api=getattr(
                        vision_config, "vision_request_api", "chat_completions"
                    ),
                    vision_stream_enabled=getattr(
                        vision_config, "vision_stream_enabled", False
                    ),
                    vision_thinking_mode=bool(
                        getattr(vision_config, "vision_thinking_mode", False)
                    ),
                    vision_reasoning_effort=str(
                        getattr(vision_config, "vision_reasoning_effort", "") or ""
                    ),
                    request_trace_id=(
                        request_trace_id if collector is not None else None
                    ),
                    collector=collector,
                )
                use_vision_tool = True
        # native：任务起点批量安全下载并校验，data URI 直接进入聊天
        # 多模态输入，不暴露 read_image 工具。
        elif combined_sources:
            aligned_images = await download_images_as_base64_aligned(
                combined_sources,
                image_policy.download,
            )
            # 进入下载范围的有效图片数（超出 max_images 被截断丢弃的部分不计
            # 入总数与失败数，与 delegated 会话 total_count 语义一致）。
            effective_total = min(
                len(combined_sources), image_policy.download.max_images
            )
            native_download_failures = sum(
                1
                for image in aligned_images[:effective_total]
                if image is None
            )
            reply_boundary = len(reply_sources)
            reply_image_urls = [
                image
                for image in aligned_images[:reply_boundary]
                if image is not None
            ] or None
            base64_image_urls = [
                image
                for image in aligned_images[reply_boundary:]
                if image is not None
            ] or None
            if effective_total > 0 and not reply_image_urls and not base64_image_urls:
                # TSK-196 复审：不在此直接 raise（该 frame 仍持有 raw locals）；
                # 置位后跳过主 LLM，经末尾单点安全抛出点清空敏感 locals 再抛。
                _image_failure_summary = _native_failure_summary(
                    total_images=effective_total,
                    download_failures=native_download_failures,
                    provider_failed=False,
                    all_unavailable=True,
                )

        if _image_failure_summary is None:
            use_search_tool = bool(
                komari_search_plugin.is_search_available(
                    caller_user_id=message.user_id,
                    caller_group_id=message.group_id,
                    caller_is_superuser=caller_is_superuser,
                )
            )
            use_fetch_tool = bool(
                komari_search_plugin.is_fetch_available(
                    caller_user_id=message.user_id,
                    caller_group_id=message.group_id,
                    caller_is_superuser=caller_is_superuser,
                )
            )
            allowed_profile_user_ids = {message.user_id}
            allowed_profile_user_ids.update(
                item.user_id
                for item in recent_messages
                if not item.is_bot and item.group_id == message.group_id and item.user_id
            )
            if (
                reply_context is not None
                and reply_context.source_side == "user"
                and reply_context.user_id
            ):
                allowed_profile_user_ids.add(reply_context.user_id)

            if reply_context_requested:
                logger.info(
                    "[KomariMemory] 引用上下文追踪: group={} message={} enabled={} side={} text_chars={} image_count={} visible_sources={} refetched={} downloaded_images={}",
                    message.group_id,
                    message.message_id,
                    reply_context is not None,
                    reply_context.source_side if reply_context else "-",
                    len(reply_context.text) if reply_context else 0,
                    reply_context.image_count if reply_context else 0,
                    len(reply_context.image_sources) if reply_context else 0,
                    reply_context_refetched,
                    len(reply_image_urls or []),
                )

            if image_urls or reply_image_urls or image_session is not None:
                quoted_viewable = (
                    image_session.quoted_count
                    if image_session is not None
                    else len(reply_image_urls or [])
                )
                current_viewable = (
                    image_session.current_count
                    if image_session is not None
                    else len(base64_image_urls or [])
                )
                base64_chars = (
                    0
                    if image_session is not None
                    else sum(len(url) for url in (reply_image_urls or []))
                    + sum(len(url) for url in (base64_image_urls or []))
                )
                logger.info(
                    "[KomariMemory] 多模态回复追踪: trace_id={} group={} message={} quoted_images={} quoted_downloaded_images={} original_images={} downloaded_images={} plaintext_chars={} base64_chars={} memories={} image_mode={} delegated_tool={}",
                    request_trace_id,
                    message.group_id,
                    message.message_id,
                    reply_context.image_count if reply_context else 0,
                    quoted_viewable,
                    len(image_urls or []),
                    current_viewable,
                    len(message.content),
                    base64_chars,
                    len(memories),
                    image_policy.mode,
                    use_vision_tool,
                )

            try:
                favorability = await user_data_plugin.get_user_favorability(message.user_id)
            except Exception as exc:
                logger.warning("[KomariChat] 获取当前好感度失败，终止本次回复: {}", exc)
                raise _FavorabilityReadError(str(exc)) from exc

            prompt_messages = await build_prompt(
                user_message=message.content,
                search_query=rewritten_query,
                memories=memories,
                config=config,
                recent_messages=recent_messages,
                current_user_id=message.user_id,
                current_user_nickname=message.user_nickname,
                memory_service=self.memory,
                group_id=message.group_id,
                image_urls=base64_image_urls if image_session is None else None,
                reply_context=reply_context,
                reply_image_urls=reply_image_urls if image_session is None else None,
                query_embedding=query_embedding,
                favorability=favorability,
                current_user_profile=current_user_profile,
                interaction_records=interaction_records,
                interaction_memories=interaction_memories,
                delegated_image_mode=use_vision_tool,
                delegated_quoted_image_count=(
                    image_session.quoted_count if image_session is not None else None
                ),
                delegated_current_image_count=(
                    image_session.current_count if image_session is not None else None
                ),
                search_tool_mode=use_search_tool,
                fetch_tool_mode=use_fetch_tool,
            )

            tools: list[dict[str, Any]] = [READ_PROFILE_TOOL, RECORD_FAVORABILITY_DELTA_TOOL]
            if use_vision_tool:
                tools.append(READ_IMAGE_TOOL)
            if use_search_tool:
                tools.append(SEARCH_WEB_TOOL)
            if use_fetch_tool:
                tools.append(FETCH_PAGE_TOOL)

            try:
                if tools:
                    # TSK-195：委托模式的主循环只经 image_session 触达图片；
                    # 会话在任务起点构建（零预下载）并在任务结束后关闭。
                    reply_result = await generate_reply_with_tools(
                        config=config,
                        messages=prompt_messages,
                        tools=tools,
                        request_trace_id=request_trace_id,
                        image_session=image_session if use_vision_tool else None,
                        memory_service=self.memory,
                        group_id=message.group_id,
                        allowed_profile_user_ids=frozenset(allowed_profile_user_ids),
                        caller_user_id=message.user_id,
                        caller_group_id=message.group_id,
                        caller_is_superuser=caller_is_superuser,
                        max_favorability_delta=user_data_plugin.get_config().max_favorability_delta_per_reply,
                        collector=collector,
                        parent_call_id=f"core-{uuid.uuid4().hex[:8]}",
                        agent_budget=agent_budget,
                    )
                else:
                    reply_result = await generate_reply(
                        config=config,
                        messages=prompt_messages,
                        request_trace_id=request_trace_id,
                        collector=collector,
                        parent_call_id=f"core-{uuid.uuid4().hex[:8]}",
                        agent_budget=agent_budget,
                    )
            except NativeMultimodalRequestError:
                # TSK-196 复审：只在 except 内直接构造安全摘要并赋值（不 raise，
                # 避免经 __context__ 保留 marker → 原 provider 异常对象链；也不
                # 引用原异常对象，只取已算好的计数/总数），统一经末尾单点安全
                # 抛出点抛 ImageUnderstandingFailureError，最终异常的 cause/context
                # 都为 None。只包装“主 provider 多模态调用失败”的窄 marker，绝不
                # 切 delegated；其他异常（MaxRounds/工具预算/协议校验/内部错误）
                # 即便 native 有图也原样传播，不误报为图片失败。delegated 的主
                # 循环已自行以 ImageUnderstandingFailureError 终止。
                _image_failure_summary = _native_failure_summary(
                    total_images=effective_total,
                    download_failures=native_download_failures,
                    provider_failed=True,
                    all_unavailable=False,
                )
            except ImageUnderstandingFailureError as exc:
                # TSK-196 复审：delegated 全部可用索引均已尝试且失败——工具循环
                # 已以专用安全异常终止；只在 except 内保存其安全摘要（不 raise，
                # 避免经 __context__ 保留原异常对象链），离开 except/finally 后
                # 经末尾单点安全抛出点抛新异常（cause/context 都为 None）。其他
                # 异常（MaxRounds/工具预算/协议/内部）即便 delegated 有图也原样
                # 传播，不误报为图片失败。
                _image_failure_summary = exc.summary
            finally:
                # TSK-195：delegated 会话持有的下载连接随任务结束释放（幂等，
                # 可重复调用；构造即零预下载，未读取也不持有 open 连接）。
                if image_session is not None:
                    await image_session.close()

        # 单点安全抛出：所有预期图片失败（native 全下载失败 / native provider
        # 失败 / delegated 全部不可用）统一在此清空本帧图片敏感 locals 后抛新
        # 异常。抛前显式重绑函数参数 ``image_urls``/``reply_context``、来源与
        # base64 列表、含 image_url 部件的 ``prompt_messages``、已 close 的
        # ``image_session`` 与可能持 raw input_data 的 ``collector``；只重绑
        # 本地名字，不触碰调用者传入的对象本体。最终异常 traceback 的
        # ``_generate_reply_core`` frame 递归投影不含原 URL/base64/视觉描述；
        # 不依赖 Sentry sanitizer 后处理。保留 safe summary/trace id/计数。
        if _image_failure_summary is not None:
            image_urls = None
            reply_context = None
            reply_sources = []
            current_sources = []
            combined_sources = []
            aligned_images = None
            reply_image_urls = None
            base64_image_urls = None
            prompt_messages = []
            if image_session is not None:
                await image_session.close()
                image_session = None
            collector = None
            raise ImageUnderstandingFailureError(_image_failure_summary)

        assert reply_result is not None

        if (reply_image_urls or base64_image_urls) and native_download_failures:
            # TSK-196：native 部分下载失败且任务成功 → 聚合摘要附加到结果。
            reply_result = replace(
                reply_result,
                image_failure_summary=_native_failure_summary(
                    total_images=effective_total,
                    download_failures=native_download_failures,
                    provider_failed=False,
                    all_unavailable=False,
                ),
            )

        logger.info(
            "[KomariChat] 生成回复成功: len={} favorability_delta={}",
            len(reply_result.content),
            reply_result.favorability_delta,
        )
        return reply_result

    async def _attempt_reply(
        self,
        *,
        message: MessageSchema,
        reply_to_message_id: str,
        image_urls: list[str] | None,
        reply_context: ReplyContext | None,
        reply_context_requested: bool,
        reply_context_refetched: bool,
        force_reply: bool,
        reason: AttemptReplyReason,
        reply_score: float | None,
        store_current: bool,
        bot_self_id: str,
        adapter_name: str,
        caller_is_superuser: bool = False,
        on_reply_triggered: ReplyTriggeredCallback | None = None,
    ) -> tuple[PendingReply | None, bool, ReplyFailureInfo | None]:
        """尝试生成并返回回复。

        Returns:
            (回复结果, 当前消息是否已存储, 失败诊断信息)
            失败诊断信息仅在确实尝试回复但失败时返回；
            频控冷却/超限/重复等正常控制流返回 (None, False, None)。
        """
        config = get_config()
        memory_config = get_memory_config()
        reservation_id: str | None = None
        request_trace_id = f"chat-{message.message_id}"

        lease: ProactiveLease | None = None
        if not force_reply:
            if not config.proactive_enabled:
                return None, False, None

            reservation_id = str(message.message_id)
            try:
                reserve_result = await self.proactive_reservation.reserve(
                    message.group_id,
                    reservation_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return None, False, ReplyFailureInfo(
                    stage="reserve",
                    error_type=type(exc).__name__,
                    summary=str(exc),
                    request_trace_id=request_trace_id,
                    reaction_sent=False,
                )
            if isinstance(reserve_result, ReservationDenied):
                match reserve_result.reason:
                    case "cooldown":
                        logger.debug("[KomariChat] 主动回复冷却或生成预占中")
                    case "rate_limited":
                        logger.debug("[KomariChat] 主动回复频率超限")
                    case "duplicate":
                        logger.debug("[KomariChat] 主动回复消息已预占或已送达")
                    case _:
                        return None, False, ReplyFailureInfo(
                            stage="reserve",
                            error_type="UnknownReservationStatusError",
                            summary=f"未知的主动回复预占状态: {reserve_result.reason}",
                            request_trace_id=request_trace_id,
                            reaction_sent=False,
                        )
                return None, False, None
            lease = reserve_result

        async def _attempt_generation() -> (  # noqa: PLR0911
            tuple[PendingReply | None, bool, ReplyFailureInfo | None]
        ):
            """在租约持有期内完成读取/生成，生成完成后内联续租裁决并移交凭据。

            生成期失败由租约 ``async with`` 退出协议自动释放，本函数不持有
            任何预占对象；强制回复（无租约）直接走同一生成路径。
            """
            reaction_sent = False

            # === 读取已有缓冲 ===
            try:
                recent_messages, interaction_records, stored = await self._read_buffers(
                    group_id=message.group_id,
                    user_id=message.user_id,
                    message=message,
                    store_current=store_current,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return None, False, ReplyFailureInfo(
                    stage="read_buffers",
                    error_type=type(exc).__name__,
                    summary=str(exc),
                    request_trace_id=request_trace_id,
                    reaction_sent=False,
                )

            # === 生成前贴出“生成中”表情（与生成并列，fire-and-forget） ===
            reaction_sent = self._schedule_reply_reaction(on_reply_triggered)

            # === 纯读取/生成核心 ===
            collector = agent_run_logger_plugin.create_collector(
                run_type="chat_reply",
                task_kind="chat_reply",
                trace_id=request_trace_id,
                input_data={
                    "message": message,
                    "recent_messages": recent_messages,
                    "interaction_records": interaction_records,
                    "image_urls": image_urls,
                    "reply_context": reply_context,
                    "reason": reason,
                    "reply_score": reply_score,
                    "force_reply": force_reply,
                },
            )
            try:
                reply_result = await self._generate_reply_core(
                    message=message,
                    recent_messages=recent_messages,
                    interaction_records=interaction_records,
                    image_urls=image_urls,
                    reply_context=reply_context,
                    reply_context_requested=reply_context_requested,
                    reply_context_refetched=reply_context_refetched,
                    request_trace_id=request_trace_id,
                    caller_is_superuser=caller_is_superuser,
                    collector=collector,
                )
            except asyncio.CancelledError as exc:
                await agent_run_logger_plugin.finalize_collector(
                    collector,
                    status="cancelled",
                    error=exc,
                )
                raise
            except _FavorabilityReadError as exc:
                await agent_run_logger_plugin.finalize_collector(
                    collector,
                    status="error",
                    error=exc,
                )
                return None, stored, ReplyFailureInfo(
                    stage="generate",
                    error_type=type(exc).__name__,
                    summary=str(exc),
                    request_trace_id=request_trace_id,
                    reaction_sent=reaction_sent,
                )
            except Exception as exc:
                await agent_run_logger_plugin.finalize_collector(
                    collector,
                    status="error",
                    error=exc,
                )
                return None, stored, ReplyFailureInfo(
                    stage="generate",
                    error_type=type(exc).__name__,
                    summary=str(exc),
                    request_trace_id=request_trace_id,
                    reaction_sent=reaction_sent,
                    image_failure_summary=(
                        exc.summary
                        if isinstance(exc, ImageUnderstandingFailureError)
                        else None
                    ),
                )
            else:
                await agent_run_logger_plugin.finalize_collector(
                    collector,
                    status="success",
                    output=reply_result,
                )

            reply = reply_result.content
            if not reply:
                logger.warning(
                    "[KomariMemory] 回复生成失败: group={} reason={} score={}",
                    message.group_id,
                    reason,
                    f"{reply_score:.3f}" if reply_score is not None else "-",
                )
                return None, stored, ReplyFailureInfo(
                    stage="generate",
                    error_type="EmptyReplyError",
                    summary="LLM 返回空回复",
                    request_trace_id=request_trace_id,
                    reaction_sent=reaction_sent,
                )

            if reply_result.favorability_delta is None:
                logger.warning("[KomariChat] 回复缺少好感度变化记录，按生成失败处理")
                return None, stored, ReplyFailureInfo(
                    stage="generate",
                    error_type="FavorabilityDeltaMissingError",
                    summary="回复缺少好感度变化记录",
                    request_trace_id=request_trace_id,
                    reaction_sent=reaction_sent,
                )

            # === 生成完成：内联一次续租裁决，成功后才移交窄凭据 ===
            if lease is not None:
                try:
                    handoff = await lease.handoff()
                except ReservationLostError:
                    logger.warning(
                        "[KomariChat] 主动回复生成完成时预占租约已丢失，取消发送"
                    )
                    return None, stored, ReplyFailureInfo(
                        stage="generate",
                        error_type="ProactiveReservationLostError",
                        summary="生成完成时主动回复预占租约已丢失",
                        request_trace_id=request_trace_id,
                        reaction_sent=reaction_sent,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    return None, stored, ReplyFailureInfo(
                        stage="generate",
                        error_type=type(exc).__name__,
                        summary=str(exc),
                        request_trace_id=request_trace_id,
                        reaction_sent=reaction_sent,
                    )
            else:
                handoff = None

            logger.info(
                "[KomariMemory] 回复生成完成，等待发送: group={} reason={} score={}",
                message.group_id,
                reason,
                f"{reply_score:.3f}" if reply_score is not None else "-",
            )
            pending_reply = PendingReply(
                reply=reply,
                reply_to_message_id=reply_to_message_id,
                message=message,
                reply_result=reply_result,
                force_reply=force_reply,
                bot_nickname=memory_config.bot_nickname,
                bot_self_id=bot_self_id,
                adapter_name=adapter_name,
                reason=reason,
                reply_score=reply_score,
                fulfillment_id=self._reply_fulfillment_id(message),
                request_trace_id=request_trace_id,
                reply_timestamp=time.time(),
                proactive_reservation_id=reservation_id,
                proactive_handoff=handoff,
                reaction_sent=reaction_sent,
            )
            return pending_reply, stored, None

        if lease is not None:
            async with lease:
                return await _attempt_generation()
        return await _attempt_generation()

    async def generate_debug_reply(
        self,
        *,
        group_id: str,
        user_id: str,
        user_nickname: str,
        content: str,
        _bot: Bot | None = None,
        image_urls: list[str] | None = None,
        reply_context: ReplyContext | None = None,
        caller_is_superuser: bool = False,
        collector: LLMDiagnosticCollector | None = None,
    ) -> DebugReplyResult:
        """debug 干跑回复生成：以命令发起者身份、当前群上下文执行纯读取/生成，
        完全跳过决策引擎、表情反应、Redis push、好感度 adjust、互动写入、冷却/频控。

        Args:
            group_id: 群 ID
            user_id: 命令发起者 ID
            user_nickname: 命令发起者昵称
            content: 测试文本
            _bot: Bot 实例（用于 refetch reply；可省略）
            image_urls: 命令附图的 URL 列表
            reply_context: 引用消息上下文（如有）
            collector: 可选的诊断收集器，缺省时自行创建

        Returns:
            DebugReplyResult（reply, favorability_delta, favorability_reason,
            interaction_history, collector）

        Raises:
            RuntimeError: 底层服务未初始化
        """
        if collector is None:
            collector = agent_run_logger_plugin.create_collector(
                run_type="chat_reply",
                task_kind="chat_reply",
                trace_id=f"debug-reply-{uuid.uuid4().hex[:12]}",
                origin="debug",
                input_data={
                    "group_id": group_id,
                    "user_id": user_id,
                    "user_nickname": user_nickname,
                    "content": content,
                    "image_urls": image_urls,
                    "reply_context": reply_context,
                },
                force_collect=True,
            )
            if collector is None:
                msg = "Agent Run debug 收集器创建失败"
                raise RuntimeError(msg)
        request_trace_id = collector.request_id

        try:
            reply_context_refetched = False
            refetched_context: ReplyContext | None = None
            if (
                _bot is not None
                and reply_context is not None
                and self._should_refetch_reply_context(context=reply_context)
            ):
                refetched_context = await self._refetch_reply_context_by_message_id(
                    bot=_bot,
                    message_id=reply_context.message_id,
                )
                reply_context_refetched = True
                if refetched_context is not None:
                    reply_context = refetched_context

            # 构造测试 MessageSchema
            message = MessageSchema(
                user_id=user_id,
                user_nickname=user_nickname,
                group_id=group_id,
                content=content,
                timestamp=time.time(),
                message_id=f"debug-{uuid.uuid4().hex[:8]}",
            )

            # === 读取已有缓冲（不 store_current，不写当前消息） ===
            recent_messages, interaction_records, _stored = await self._read_buffers(
                group_id=group_id,
                user_id=user_id,
                message=message,
                store_current=False,
            )
        except asyncio.CancelledError as exc:
            await agent_run_logger_plugin.finalize_collector(
                collector,
                status="cancelled",
                error=exc,
            )
            raise
        except Exception as exc:
            collector.add_error(
                phase="debug_reply_context",
                error_type=type(exc).__name__,
                message=str(exc),
            )
            await agent_run_logger_plugin.finalize_collector(
                collector,
                status="error",
                error=exc,
            )
            raise

        # === 纯读取/生成核心 ===
        try:
            reply_result = await self._generate_reply_core(
                message=message,
                recent_messages=recent_messages,
                interaction_records=interaction_records,
                image_urls=image_urls,
                reply_context=reply_context,
                reply_context_requested=reply_context is not None,
                reply_context_refetched=reply_context_refetched,
                request_trace_id=request_trace_id,
                caller_is_superuser=caller_is_superuser,
                collector=collector,
            )
        except asyncio.CancelledError as exc:
            await agent_run_logger_plugin.finalize_collector(
                collector,
                status="cancelled",
                error=exc,
            )
            raise
        except Exception as exc:
            if isinstance(exc, FinishedException):
                raise
            collector.add_error(
                phase="generate_reply_core",
                error_type=type(exc).__name__,
                message=str(exc),
            )
            logger.warning(
                "[KomariChat] debug 回复生成失败: user={} error={}\n{}",
                user_id,
                exc,
                traceback.format_exc(),
            )
            await agent_run_logger_plugin.finalize_collector(
                collector,
                status="error",
                error=exc,
            )
            if isinstance(exc, ImageUnderstandingFailureError):
                # TSK-196 复审：完成 collector 安全 finalize 与普通安全日志后，
                # 显式重绑本帧图片敏感 locals（image_urls/reply_context/
                # refetched_context/本地 collector）为 None 再 re-raise；保持
                # 异常类型/summary 语义，最终 debug 异常 traceback 的
                # komari_chat 帧递归投影不含 raw URL/base64。普通非图片 debug
                # 错误语义不变。
                image_urls = None
                reply_context = None
                refetched_context = None
                collector = None
            raise
        result = DebugReplyResult(
            reply=reply_result.content,
            reply_to_message_id=(
                reply_context.message_id if reply_context is not None else None
            ),
            favorability_delta=reply_result.favorability_delta,
            favorability_reason=reply_result.favorability_reason,
            interaction_history=reply_result.interaction_history,
            collector=collector,
        )
        await agent_run_logger_plugin.finalize_collector(
            collector,
            status="success",
            output=reply_result,
        )
        return result
