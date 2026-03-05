"""Komari Memory 消息处理核心。"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Literal

from nonebot import logger

from komari_bot.plugins.komari_memory.services.config_interface import get_config
from komari_bot.plugins.komari_memory.services.message_filter import preprocess_message
from komari_bot.plugins.komari_memory.services.redis_manager import (
    MessageSchema,
    RedisManager,
)
from komari_bot.plugins.komari_memory.services.social_timing_service import (
    SocialTimingService,
)
from komari_bot.plugins.komari_memory.services.unified_candidate_rerank import (
    UnifiedCandidateRerankService,
    UnifiedRerankResult,
)

from ..services.image_downloader import download_images_as_base64
from ..services.llm_service import generate_reply
from ..services.not_related_logger import is_not_related, log_not_related
from ..services.prompt_builder import build_prompt
from ..services.query_rewrite_service import QueryRewriteService

if TYPE_CHECKING:
    from nonebot.adapters.onebot.v11 import GroupMessageEvent

    from komari_bot.plugins.komari_memory.services.memory_service import MemoryService
    from komari_bot.plugins.komari_memory.services.scene_runtime_service import (
        SceneRuntimeService,
    )

CallIntent = Literal["none", "ambiguous", "direct_call", "casual_mention"]
ReplyReason = Literal["at", "direct_call", "score"]
MemoryAction = Literal["store", "drop"]
ReplyAction = Literal[
    "replied",
    "replied_forced",
    "not_replied",
    "not_related",
    "generation_failed",
]


class MessageHandler:
    """消息处理核心。"""

    _DIRECT_CALL_BONUS = 0.25
    _CASUAL_MENTION_PENALTY = -0.18

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
        self.unified_rerank = UnifiedCandidateRerankService(runtime_service=scene_runtime)
        self.social_timing = SocialTimingService(redis)

    def _is_at_trigger(self, event: GroupMessageEvent) -> bool:
        """检查是否 @ 了机器人。"""
        return bool(hasattr(event, "to_me") and event.to_me)

    @staticmethod
    def _clamp_score(value: float) -> float:
        return max(0.0, min(1.0, value))

    def _resolve_call_intent(
        self, rank_result: UnifiedRerankResult
    ) -> tuple[CallIntent, float]:
        """根据 CALL_* 分数判定呼叫意图。"""
        config = get_config()

        if (
            not rank_result.alias_hit
            or rank_result.call_direct_score is None
            or rank_result.call_mention_score is None
        ):
            return "none", 0.0

        call_margin = rank_result.call_direct_score - rank_result.call_mention_score
        threshold = config.call_margin_threshold
        if call_margin >= threshold:
            return "direct_call", call_margin
        if call_margin <= -threshold:
            return "casual_mention", call_margin
        return "ambiguous", call_margin

    def _should_drop_memory(self, rank_result: UnifiedRerankResult) -> bool:
        """根据 NOISE/MEANINGFUL 判定是否丢弃记忆。"""
        config = get_config()
        noise_delta = rank_result.noise_score - rank_result.meaningful_score
        return (
            rank_result.noise_score >= config.noise_conf_threshold
            and noise_delta >= config.noise_margin_threshold
        )

    def _get_alias_adjust(self, intent: CallIntent) -> float:
        """获取意图对回复分的修正值。"""
        if intent == "direct_call":
            return self._DIRECT_CALL_BONUS
        if intent == "casual_mention":
            return self._CASUAL_MENTION_PENALTY
        return 0.0

    @staticmethod
    def _safe_round(value: float | None) -> float | None:
        if value is None:
            return None
        return round(value, 4)

    def _log_decision(self, payload: dict[str, object]) -> None:
        """输出决策日志（info 摘要 + debug 完整结构）。"""
        logger.info(
            "[KomariMemory] decision_summary group=%s user=%s msg=%s "
            "memory=%s reply=%s reason=%s intent=%s scene=%s "
            "reply_score=%s timing=%s",
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
            "[KomariMemory] decision_full=%s",
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )

    async def process_message(
        self,
        event: GroupMessageEvent,
    ) -> dict[str, str] | None:
        """处理群聊消息的主流程。"""
        config = get_config()

        user_id = str(event.user_id)
        group_id = str(event.group_id)
        message_content = event.get_plaintext()
        message_id = str(event.message_id)

        image_urls = [
            seg.data["url"]
            for seg in event.message
            if seg.type == "image" and seg.data.get("url")
        ]
        if image_urls:
            logger.info("[KomariMemory] 检测到 %s 张图片", len(image_urls))

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

        # 1) @ 强制回复，无视冷却/频控，跳过评分链路
        if self._is_at_trigger(event):
            reply, _stored = await self._attempt_reply(
                message=message,
                reply_to_message_id=message_id,
                image_urls=image_urls,
                force_reply=True,
                reason="at",
                reply_score=None,
                store_current=True,
            )
            self._log_decision(
                {
                    "group_id": group_id,
                    "user_id": user_id,
                    "message_id": message_id,
                    "alias_hit": None,
                    "call_intent": "none",
                    "memory_action": "store",
                    "reply_action": "replied_forced" if reply else "generation_failed",
                    "forced_reply_reason": "at",
                    "reply_score": None,
                    "timing_score": None,
                    "scene_score": None,
                    "best_scene_id": None,
                    "noise_score": None,
                    "meaningful_score": None,
                }
            )
            return reply

        # 2) 预过滤
        filter_result = await preprocess_message(
            message=message_content,
            config=config,
            redis=self.redis,
            group_id=group_id,
        )
        if filter_result.should_skip:
            logger.debug(
                "[KomariMemory] 消息被过滤: %s - %s...",
                filter_result.reason,
                message_content[:30],
            )
            await self._handle_low_value(message)
            self._log_decision(
                {
                    "group_id": group_id,
                    "user_id": user_id,
                    "message_id": message_id,
                    "alias_hit": None,
                    "call_intent": "none",
                    "memory_action": "drop",
                    "reply_action": "not_replied",
                    "forced_reply_reason": "none",
                    "filter_reason": filter_result.reason,
                    "reply_score": None,
                    "timing_score": None,
                    "scene_score": None,
                    "best_scene_id": None,
                    "noise_score": None,
                    "meaningful_score": None,
                }
            )
            return None

        # 3) 单次 rerank 统一候选评分
        rank_result = await self.unified_rerank.rank_message(message_content)
        memory_drop = self._should_drop_memory(rank_result)
        memory_store = not memory_drop
        memory_action: MemoryAction = "store" if memory_store else "drop"
        call_intent, call_margin = self._resolve_call_intent(rank_result)

        # 4) 时机分（仅影响回复）
        timing_result = await self.social_timing.score(
            group_id=group_id,
            alias_hit=rank_result.alias_hit,
        )
        scene_score = rank_result.best_scene_score
        alias_adjust = self._get_alias_adjust(call_intent)
        reply_score = self._clamp_score(
            (1.0 - config.timing_weight) * scene_score
            + config.timing_weight * timing_result.timing_score
            + alias_adjust
        )

        reply_action: ReplyAction = "not_replied"
        forced_reply_reason: ReplyReason | Literal["none"] = "none"

        # 5) direct_call 强制回复（仍计算并记录评分）
        if call_intent == "direct_call":
            forced_reply_reason = "direct_call"
            reply, stored = await self._attempt_reply(
                message=message,
                reply_to_message_id=message_id,
                image_urls=image_urls,
                force_reply=True,
                reason="direct_call",
                reply_score=reply_score,
                store_current=memory_store,
            )
            if reply is not None:
                reply_action = "replied_forced"
                self._log_decision(
                    {
                        "group_id": group_id,
                        "user_id": user_id,
                        "message_id": message_id,
                        "alias_hit": rank_result.alias_hit,
                        "call_intent": call_intent,
                        "call_margin": self._safe_round(call_margin),
                        "memory_action": memory_action,
                        "reply_action": reply_action,
                        "forced_reply_reason": forced_reply_reason,
                        "reply_score": self._safe_round(reply_score),
                        "timing_score": self._safe_round(timing_result.timing_score),
                        "scene_score": self._safe_round(scene_score),
                        "best_scene_id": rank_result.best_scene_id,
                        "noise_score": self._safe_round(rank_result.noise_score),
                        "meaningful_score": self._safe_round(
                            rank_result.meaningful_score
                        ),
                        "call_direct_score": self._safe_round(
                            rank_result.call_direct_score
                        ),
                        "call_mention_score": self._safe_round(
                            rank_result.call_mention_score
                        ),
                    }
                )
                return reply
            if memory_store and not stored:
                await self._handle_normal_message(message)
            reply_action = "generation_failed"
        else:
            # 6) 普通回复路径
            should_reply = reply_score >= config.reply_threshold
            if should_reply:
                reply, stored = await self._attempt_reply(
                    message=message,
                    reply_to_message_id=message_id,
                    image_urls=image_urls,
                    force_reply=False,
                    reason="score",
                    reply_score=reply_score,
                    store_current=memory_store,
                )
                if reply is not None:
                    reply_action = "replied"
                    self._log_decision(
                        {
                            "group_id": group_id,
                            "user_id": user_id,
                            "message_id": message_id,
                            "alias_hit": rank_result.alias_hit,
                            "call_intent": call_intent,
                            "call_margin": self._safe_round(call_margin),
                            "memory_action": memory_action,
                            "reply_action": reply_action,
                            "forced_reply_reason": forced_reply_reason,
                            "reply_score": self._safe_round(reply_score),
                            "timing_score": self._safe_round(
                                timing_result.timing_score
                            ),
                            "scene_score": self._safe_round(scene_score),
                            "best_scene_id": rank_result.best_scene_id,
                            "noise_score": self._safe_round(rank_result.noise_score),
                            "meaningful_score": self._safe_round(
                                rank_result.meaningful_score
                            ),
                            "call_direct_score": self._safe_round(
                                rank_result.call_direct_score
                            ),
                            "call_mention_score": self._safe_round(
                                rank_result.call_mention_score
                            ),
                        }
                    )
                    return reply
                if memory_store and not stored:
                    await self._handle_normal_message(message)
                reply_action = "generation_failed"
            elif memory_store:
                await self._handle_normal_message(message)
            else:
                await self._handle_low_value(message)

        self._log_decision(
            {
                "group_id": group_id,
                "user_id": user_id,
                "message_id": message_id,
                "alias_hit": rank_result.alias_hit,
                "call_intent": call_intent,
                "call_margin": self._safe_round(call_margin),
                "memory_action": memory_action,
                "reply_action": reply_action,
                "forced_reply_reason": forced_reply_reason,
                "reply_score": self._safe_round(reply_score),
                "timing_score": self._safe_round(timing_result.timing_score),
                "scene_score": self._safe_round(scene_score),
                "best_scene_id": rank_result.best_scene_id,
                "noise_score": self._safe_round(rank_result.noise_score),
                "meaningful_score": self._safe_round(rank_result.meaningful_score),
                "call_direct_score": self._safe_round(rank_result.call_direct_score),
                "call_mention_score": self._safe_round(rank_result.call_mention_score),
            }
        )
        return None

    async def _handle_low_value(self, message: MessageSchema) -> None:
        """处理低价值消息（直接丢弃，不存储）。"""
        logger.debug("[KomariMemory] 低价值消息已丢弃: %s...", message.content[:30])

    async def _handle_normal_message(self, message: MessageSchema) -> None:
        """处理普通消息（存储缓冲并计数）。"""
        await self.redis.push_message(message.group_id, message)
        await self.redis.increment_message_count(message.group_id)
        token_count = len(message.content)
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
        logger.debug("[KomariMemory] AI 回复已存储: %s...", reply_content[:30])

    async def _attempt_reply(
        self,
        *,
        message: MessageSchema,
        reply_to_message_id: str,
        image_urls: list[str] | None,
        force_reply: bool,
        reason: ReplyReason,
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
            logger.warning("[KomariMemory] 预生成查询特征向量失败: %s", e)
            query_embedding = None

        memories = await self.memory.search_conversations(
            query=rewritten_query,
            group_id=message.group_id,
            user_id=message.user_id,
            limit=config.memory_search_limit,
            query_embedding=query_embedding,
        )

        base64_image_urls = None
        if image_urls:
            base64_image_urls = await download_images_as_base64(image_urls)

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
        )
        if reply is None:
            logger.warning(
                "[KomariMemory] 回复生成失败: group=%s reason=%s score=%s",
                message.group_id,
                reason,
                f"{reply_score:.3f}" if reply_score is not None else "-",
            )
            return None, stored

        if is_not_related(reply):
            logger.info(
                "[KomariMemory] not related: group=%s reason=%s score=%s",
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
            "[KomariMemory] 回复成功: group=%s reason=%s score=%s",
            message.group_id,
            reason,
            f"{reply_score:.3f}" if reply_score is not None else "-",
        )
        return {"reply": reply, "reply_to_message_id": reply_to_message_id}, stored
