"""Komari Memory 消息处理核心。"""

from __future__ import annotations

import json
import re
import time
import traceback
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from nonebot import logger
from nonebot.adapters.onebot.v11.event import Reply
from nonebot.compat import type_validate_python
from nonebot.exception import FinishedException
from nonebot.plugin import require

from komari_bot.plugins.komari_decision.services.decision_engine import (
    DecisionEngine,
    DecisionOutcome,
)
from komari_bot.plugins.komari_memory.services.config_interface import get_config
from komari_bot.plugins.komari_memory.services.redis_manager import (
    MessageSchema,
    RedisManager,
)
from komari_bot.plugins.llm_provider.config_schema import DynamicConfigSchema
from komari_bot.plugins.llm_provider.diagnostic import LLMDiagnosticCollector

from ..services.image_downloader import download_images_as_base64, extract_image_sources
from ..services.llm_service import (
    READ_IMAGE_TOOL,
    READ_PROFILE_TOOL,
    RECORD_FAVORABILITY_DELTA_TOOL,
    TAVILY_SEARCH_TOOL,
    InteractionHistoryRecord,
    ReplyResult,
    generate_reply,
    generate_reply_with_tools,
)
from ..services.prompt_builder import build_prompt
from ..services.query_rewrite_service import QueryRewriteService
from ..services.reply_context import ReplyContext

user_data_plugin = require("user_data")

config_manager_plugin = require("config_manager")
komari_search_plugin = require("komari_search")
llm_provider_config_manager = config_manager_plugin.get_config_manager(
    "llm_provider",
    DynamicConfigSchema,
)

if TYPE_CHECKING:
    from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent

    from komari_bot.plugins.komari_decision.services.scene_runtime_service import (
        SceneRuntimeService,
    )
    from komari_bot.plugins.komari_memory.services.memory_service import MemoryService

AttemptReplyReason = Literal["at", "direct_call", "score"]
ReplyAction = Literal[
    "replied",
    "replied_forced",
    "not_replied",
    "generation_failed",
    "blocked_by_user_ban",
]
ReplyTriggeredCallback = Callable[[], Awaitable[None]]


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
    """已生成但尚未确认送达的回复及其待提交副作用。"""

    reply: str
    reply_to_message_id: str
    message: MessageSchema
    reply_result: ReplyResult
    force_reply: bool
    bot_nickname: str
    reason: AttemptReplyReason
    reply_score: float | None
    on_reply_triggered: ReplyTriggeredCallback | None = None
    decision_payload: dict[str, object] | None = None


class _FavorabilityReadError(RuntimeError):
    """读取当前好感度失败。"""


class MessageHandler:
    """消息处理核心。"""

    def __init__(
        self,
        redis: RedisManager,
        memory: MemoryService,
        scene_runtime: SceneRuntimeService | None = None,
    ) -> None:
        """初始化消息处理器。"""
        self.redis = redis
        self.memory = memory
        self.query_rewrite = QueryRewriteService()
        self.decision_engine = DecisionEngine(redis, scene_runtime)

    def _is_at_trigger(self, event: GroupMessageEvent) -> bool:
        """检查是否 @ 了机器人。"""
        return bool(hasattr(event, "to_me") and event.to_me)

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
        if self._is_at_trigger(event):
            return True, message_content

        config = get_config()
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
                    reply_action="not_replied",
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

        reason: AttemptReplyReason = (
            outcome.reply_reason if outcome.reply_reason != "none" else "score"
        )
        pending_reply, stored = await self._attempt_reply(
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
            on_reply_triggered=on_reply_triggered,
        )
        if pending_reply is not None:
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
        return None

    async def _handle_low_value(self, message: MessageSchema) -> None:
        """处理低价值消息（直接丢弃，不存储）。"""
        logger.debug("[KomariMemory] 低价值消息已丢弃: {}...", message.content[:30])

    async def _handle_normal_message(self, message: MessageSchema) -> None:
        """处理普通消息（连续追加到当前会话缓冲区）。"""
        await self.redis.push_message(message.group_id, message)

    async def _store_ai_reply(
        self,
        group_id: str,
        reply_content: str,
        bot_nickname: str,
    ) -> None:
        """存储 AI 回复到缓冲区。"""
        bot_message = MessageSchema(
            user_id="bot",
            user_nickname=bot_nickname,
            group_id=group_id,
            content=reply_content,
            timestamp=time.time(),
            message_id=f"bot_{uuid.uuid4().hex[:16]}",
            is_bot=True,
        )

        await self.redis.push_message(group_id, bot_message)
        logger.debug("[KomariMemory] AI 回复已存储: {}...", reply_content[:30])

    async def _write_interaction_history(
        self,
        *,
        message: MessageSchema,
        new_record: InteractionHistoryRecord,
        lock_timeout_seconds: int | None,
    ) -> None:
        """将本轮互动写入跨群 Redis 原始缓冲。"""
        del lock_timeout_seconds
        config = get_config()
        if not config.global_interaction_enabled:
            return

        global_record = {
            "version": 1,
            "event": str(new_record.get("event", "")).strip(),
            "result": str(new_record.get("result", "")).strip(),
            "emotion": str(new_record.get("emotion", "")).strip(),
            "display_name": self._resolve_display_name(message),
            "timestamp": time.time(),
            "message_id": message.message_id,
        }
        await self.redis.push_global_interaction(
            user_id=message.user_id,
            record=global_record,
            trigger_size=config.global_interaction_trigger_size,
        )

    @staticmethod
    def _resolve_display_name(message: MessageSchema) -> str:
        """解析写入跨群互动缓冲的用户显示名。"""
        return str(message.user_nickname or message.user_id).strip() or message.user_id

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
        config = get_config()
        stored = False

        recent_messages = await self.redis.get_buffer(
            group_id, limit=config.summary_max_buffer_size
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
        _reason: AttemptReplyReason,
        _reply_score: float | None,
        request_trace_id: str,
        collector: LLMDiagnosticCollector | None = None,
    ) -> ReplyResult:
        """纯读取/生成核心：查询重写、记忆/画像/好感度读取、prompt 构建、LLM 回复生成。

        不执行任何副作用：不写 Redis、不调好感度、不写互动历史、不设冷却。
        """
        config = get_config()

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
            from nonebot.plugin import require

            embedding_provider = require("embedding_provider")
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

        base64_image_urls: list[str] | None = None
        if image_urls:
            base64_image_urls = await download_images_as_base64(image_urls)
        reply_image_urls: list[str] | None = None
        if reply_context and reply_context.image_sources:
            reply_image_urls = await download_images_as_base64(
                list(reply_context.image_sources)
            )
        all_base64_images = (reply_image_urls or []) + (base64_image_urls or [])
        use_vision_tool = getattr(config, "vision_tool_enabled", True) and bool(
            all_base64_images
        )
        use_search_tool = bool(komari_search_plugin.is_search_available())

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

        if image_urls or reply_image_urls:
            logger.info(
                "[KomariMemory] 多模态回复追踪: trace_id={} group={} message={} quoted_images={} quoted_downloaded_images={} original_images={} downloaded_images={} plaintext_chars={} base64_chars={} memories={} vision_tool_mode={}",
                request_trace_id,
                message.group_id,
                message.message_id,
                reply_context.image_count if reply_context else 0,
                len(reply_image_urls or []),
                len(image_urls or []),
                len(base64_image_urls or []),
                len(message.content),
                sum(len(url) for url in (reply_image_urls or []))
                + sum(len(url) for url in (base64_image_urls or [])),
                len(memories),
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
            image_urls=base64_image_urls,
            reply_context=reply_context,
            reply_image_urls=reply_image_urls,
            query_embedding=query_embedding,
            favorability=favorability,
            current_user_profile=current_user_profile,
            interaction_records=interaction_records,
            interaction_memories=interaction_memories,
            vision_tool_mode=use_vision_tool,
            search_tool_mode=use_search_tool,
        )

        tools: list[dict[str, Any]] = [READ_PROFILE_TOOL, RECORD_FAVORABILITY_DELTA_TOOL]
        if use_vision_tool:
            tools.append(READ_IMAGE_TOOL)
        if use_search_tool:
            tools.append(TAVILY_SEARCH_TOOL)

        if tools:
            vision_model = ""
            vision_temperature = 0.3
            vision_max_tokens = 1024
            vision_thinking_mode = False
            vision_reasoning_effort = ""
            if use_vision_tool:
                vision_config = llm_provider_config_manager.get()
                vision_model = vision_config.vision_model
                vision_temperature = vision_config.vision_temperature
                vision_max_tokens = vision_config.vision_max_tokens
                vision_thinking_mode = vision_config.vision_thinking_mode
                vision_reasoning_effort = vision_config.vision_reasoning_effort

            reply_result = await generate_reply_with_tools(
                config=config,
                messages=prompt_messages,
                tools=tools,
                request_trace_id=request_trace_id,
                base64_images=all_base64_images if use_vision_tool else None,
                vision_model=vision_model,
                vision_temperature=vision_temperature,
                vision_max_tokens=vision_max_tokens,
                max_tool_rounds=5,
                memory_service=self.memory,
                group_id=message.group_id,
                max_favorability_delta=user_data_plugin.get_config().max_favorability_delta_per_reply,
                vision_thinking_mode=vision_thinking_mode,
                vision_reasoning_effort=vision_reasoning_effort,
                collector=collector,
                parent_call_id=f"core-{uuid.uuid4().hex[:8]}",
            )
        else:
            reply_result = await generate_reply(
                config=config,
                messages=prompt_messages,
                request_trace_id=request_trace_id,
                collector=collector,
                parent_call_id=f"core-{uuid.uuid4().hex[:8]}",
            )

        logger.info(
            "[KomariChat] 生成回复成功: len={} favorability_delta={}",
            len(reply_result.content),
            reply_result.favorability_delta,
        )
        return reply_result

    async def _commit_side_effects(
        self,
        *,
        message: MessageSchema,
        reply_result: ReplyResult,
        force_reply: bool,
        group_id: str,
        bot_nickname: str,
    ) -> None:
        """提交正常聊天副作用：好感度 adjust、AI 回复存储、互动历史写入、冷却与频控。"""
        if reply_result.favorability_delta is None:
            logger.warning("[KomariChat] 回复缺少好感度变化记录，按生成失败处理")
            msg = "favorability_delta missing"
            raise ValueError(msg)

        logger.debug(
            "[KomariChat] 准备提交好感度变化: group={} user={} delta={} reason={}",
            group_id,
            message.user_id,
            reply_result.favorability_delta,
            reply_result.favorability_reason or "-",
        )
        adjust_result = await user_data_plugin.adjust_user_favorability(
            message.user_id,
            reply_result.favorability_delta,
        )
        logger.info(
            "[KomariChat] 好感度已更新: user={} before={} delta={} after={} reason={}",
            message.user_id,
            adjust_result.before,
            adjust_result.delta,
            adjust_result.after,
            reply_result.favorability_reason or "-",
        )

        await self._store_ai_reply(
            group_id=group_id,
            reply_content=reply_result.content,
            bot_nickname=bot_nickname,
        )
        try:
            await self._write_interaction_history(
                message=message,
                new_record=reply_result.interaction_history,
                lock_timeout_seconds=get_config().memory_agent_lock_timeout_seconds,
            )
        except Exception:
            logger.debug(
                "[KomariChat] 互动历史写入失败（非致命）: user={}",
                message.user_id,
                exc_info=True,
            )
        if not force_reply:
            config = get_config()
            await self.redis.set_cooldown(group_id, config.proactive_cooldown)
            await self.redis.increment_proactive_count(group_id)

    async def commit_delivered_reply(self, pending_reply: PendingReply) -> None:
        """在回复确认送达后提交反应、决策日志及聊天副作用。"""
        if pending_reply.on_reply_triggered is not None:
            try:
                await pending_reply.on_reply_triggered()
            except Exception:
                logger.debug("[KomariChat] 回复触发表情反应回调失败", exc_info=True)

        if pending_reply.decision_payload is not None:
            self._log_decision(pending_reply.decision_payload)

        await self._commit_side_effects(
            message=pending_reply.message,
            reply_result=pending_reply.reply_result,
            force_reply=pending_reply.force_reply,
            group_id=pending_reply.message.group_id,
            bot_nickname=pending_reply.bot_nickname,
        )
        logger.info(
            "[KomariMemory] 回复已送达并提交副作用: group={} reason={} score={}",
            pending_reply.message.group_id,
            pending_reply.reason,
            (
                f"{pending_reply.reply_score:.3f}"
                if pending_reply.reply_score is not None
                else "-"
            ),
        )

    async def _attempt_reply(  # noqa: PLR0911
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
        on_reply_triggered: ReplyTriggeredCallback | None = None,
    ) -> tuple[PendingReply | None, bool]:
        """尝试生成并返回回复。

        Returns:
            (回复结果, 当前消息是否已存储)
        """
        config = get_config()

        if not force_reply:
            if not config.proactive_enabled:
                return None, False

            if await self.redis.is_on_cooldown(message.group_id):
                logger.debug("[KomariMemory] 主动回复冷却中")
                return None, False

            current_count = await self.redis.get_proactive_count(message.group_id)
            if current_count >= config.proactive_max_per_hour:
                logger.debug("[KomariMemory] 主动回复频率超限")
                return None, False

        # === 读取已有缓冲 ===
        recent_messages, interaction_records, stored = await self._read_buffers(
            group_id=message.group_id,
            user_id=message.user_id,
            message=message,
            store_current=store_current,
        )

        # === 纯读取/生成核心 ===
        request_trace_id = f"chat-{message.message_id}"
        try:
            reply_result = await self._generate_reply_core(
                message=message,
                recent_messages=recent_messages,
                interaction_records=interaction_records,
                image_urls=image_urls,
                reply_context=reply_context,
                reply_context_requested=reply_context_requested,
                reply_context_refetched=reply_context_refetched,
                _reason=reason,
                _reply_score=reply_score,
                request_trace_id=request_trace_id,
                collector=None,
            )
        except _FavorabilityReadError:
            return None, stored

        reply = reply_result.content
        if reply is None:
            logger.warning(
                "[KomariMemory] 回复生成失败: group={} reason={} score={}",
                message.group_id,
                reason,
                f"{reply_score:.3f}" if reply_score is not None else "-",
            )
            return None, stored

        if reply_result.favorability_delta is None:
            logger.warning("[KomariChat] 回复缺少好感度变化记录，按生成失败处理")
            return None, stored

        logger.info(
            "[KomariMemory] 回复生成完成，等待发送: group={} reason={} score={}",
            message.group_id,
            reason,
            f"{reply_score:.3f}" if reply_score is not None else "-",
        )
        return (
            PendingReply(
                reply=reply,
                reply_to_message_id=reply_to_message_id,
                message=message,
                reply_result=reply_result,
                force_reply=force_reply,
                bot_nickname=config.bot_nickname,
                reason=reason,
                reply_score=reply_score,
                on_reply_triggered=on_reply_triggered,
            ),
            stored,
        )

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
            collector = LLMDiagnosticCollector(
                request_id=f"debug-reply-{uuid.uuid4().hex[:12]}"
            )
        request_trace_id = collector.request_id

        reply_context_refetched = False
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
                _reason="direct_call",
                _reply_score=None,
                request_trace_id=request_trace_id,
                collector=collector,
            )
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
            raise

        return DebugReplyResult(
            reply=reply_result.content,
            reply_to_message_id=(
                reply_context.message_id if reply_context is not None else None
            ),
            favorability_delta=reply_result.favorability_delta,
            favorability_reason=reply_result.favorability_reason,
            interaction_history=reply_result.interaction_history,
            collector=collector,
        )
