"""Komari Memory 消息处理核心。"""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING, Literal

from nonebot import logger

from komari_bot.plugins.komari_decision.services.decision_engine import (
    DecisionEngine,
    DecisionOutcome,
)
from komari_bot.plugins.komari_memory.services.config_interface import get_config
from komari_bot.plugins.komari_memory.services.redis_manager import (
    MessageSchema,
    RedisManager,
)
from komari_bot.plugins.komari_memory.services.token_counter import (
    estimate_text_tokens,
)

from ..services.image_downloader import download_images_as_base64
from ..services.llm_service import generate_reply
from ..services.not_related_logger import is_not_related, log_not_related
from ..services.prompt_builder import build_prompt
from ..services.query_rewrite_service import QueryRewriteService

if TYPE_CHECKING:
    from nonebot.adapters.onebot.v11 import GroupMessageEvent

    from komari_bot.plugins.komari_decision.services.scene_runtime_service import (
        SceneRuntimeService,
    )
    from komari_bot.plugins.komari_memory.services.memory_service import MemoryService

AttemptReplyReason = Literal["at", "direct_call", "score"]
ReplyAction = Literal[
    "replied",
    "replied_forced",
    "not_replied",
    "not_related",
    "generation_failed",
]


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
        event: GroupMessageEvent,
    ) -> dict[str, str] | None:
        """处理群聊消息的主流程。"""
        user_id = str(event.user_id)
        group_id = str(event.group_id)
        at_trigger, message_content = self._resolve_trigger_message(event)
        message_id = str(event.message_id)

        image_urls = [
            seg.data["url"]
            for seg in event.message
            if seg.type == "image" and seg.data.get("url")
        ]
        if image_urls:
            logger.info("[KomariMemory] 检测到 {} 张图片", len(image_urls))

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

        reason: AttemptReplyReason = (
            outcome.reply_reason if outcome.reply_reason != "none" else "score"
        )
        reply, stored = await self._attempt_reply(
            message=message,
            reply_to_message_id=message_id,
            image_urls=image_urls,
            force_reply=outcome.force_reply,
            reason=reason,
            reply_score=outcome.reply_score,
            store_current=memory_store,
        )
        if reply is not None:
            reply_action: ReplyAction = (
                "replied_forced" if outcome.force_reply else "replied"
            )
            self._log_decision(
                self._build_decision_payload(
                    group_id=group_id,
                    user_id=user_id,
                    message_id=message_id,
                    outcome=outcome,
                    reply_action=reply_action,
                )
            )
            return reply

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
        """处理普通消息（存储缓冲并计数）。"""
        await self.redis.push_message(message.group_id, message)
        await self.redis.increment_message_count(message.group_id)
        token_count = estimate_text_tokens(message.content)
        await self.redis.increment_tokens(message.group_id, token_count)

    async def _store_ai_reply(
        self,
        group_id: str,
        reply_content: str,
        bot_nickname: str,
    ) -> None:
        """存储 AI 回复到缓冲区。"""
        import uuid

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

    async def _attempt_reply(
        self,
        *,
        message: MessageSchema,
        reply_to_message_id: str,
        image_urls: list[str] | None,
        force_reply: bool,
        reason: AttemptReplyReason,
        reply_score: float | None,
        store_current: bool,
    ) -> tuple[dict[str, str] | None, bool]:
        """尝试生成并返回回复。

        Returns:
            (回复结果, 当前消息是否已存储)
        """
        config = get_config()
        stored = False

        if not force_reply:
            if not config.proactive_enabled:
                return None, stored

            if await self.redis.is_on_cooldown(message.group_id):
                logger.debug("[KomariMemory] 主动回复冷却中")
                return None, stored

            current_count = await self.redis.get_proactive_count(message.group_id)
            if current_count >= config.proactive_max_per_hour:
                logger.debug("[KomariMemory] 主动回复频率超限")
                return None, stored

        recent_messages = await self.redis.get_buffer(
            message.group_id, limit=config.context_messages_limit
        )

        if store_current:
            await self._handle_normal_message(message)
            stored = True

        rewritten_query = await self.query_rewrite.rewrite_query(
            current_query=message.content,
            conversation_history=recent_messages,
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

        request_trace_id = f"chat-{message.message_id}"
        base64_image_urls = None
        if image_urls:
            base64_image_urls = await download_images_as_base64(image_urls)
            logger.info(
                "[KomariMemory] 多模态回复追踪: trace_id={} group={} message={} original_images={} downloaded_images={} plaintext_chars={} base64_chars={} memories={}",
                request_trace_id,
                message.group_id,
                message.message_id,
                len(image_urls),
                len(base64_image_urls),
                len(message.content),
                sum(len(url) for url in base64_image_urls),
                len(memories),
            )

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
            query_embedding=query_embedding,
        )

        reply = await generate_reply(
            config=config,
            messages=prompt_messages,
            request_trace_id=request_trace_id,
        )
        if reply is None:
            logger.warning(
                "[KomariMemory] 回复生成失败: group={} reason={} score={}",
                message.group_id,
                reason,
                f"{reply_score:.3f}" if reply_score is not None else "-",
            )
            return None, stored

        if is_not_related(reply):
            logger.info(
                "[KomariMemory] not related: group={} reason={} score={}",
                message.group_id,
                reason,
                f"{reply_score:.3f}" if reply_score is not None else "-",
            )
            await log_not_related(
                user_message=message.content,
                group_id=message.group_id,
                user_id=message.user_id,
                score=reply_score,
            )
            return None, stored

        await self._store_ai_reply(
            group_id=message.group_id,
            reply_content=reply,
            bot_nickname=config.bot_nickname,
        )
        if not force_reply:
            await self.redis.set_cooldown(message.group_id, config.proactive_cooldown)
            await self.redis.increment_proactive_count(message.group_id)

        logger.info(
            "[KomariMemory] 回复成功: group={} reason={} score={}",
            message.group_id,
            reason,
            f"{reply_score:.3f}" if reply_score is not None else "-",
        )
        return {"reply": reply, "reply_to_message_id": reply_to_message_id}, stored
