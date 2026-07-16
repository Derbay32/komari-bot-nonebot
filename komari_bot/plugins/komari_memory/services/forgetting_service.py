"""记忆忘却服务 - 定期清理和模糊化到期记忆。"""

import asyncio
import re
from collections.abc import Callable
from typing import Any

import asyncpg
from nonebot import logger
from nonebot.plugin import require

from ..config_schema import KomariMemoryConfigSchema
from ..core.retry import retry_async
from .config_interface import get_config

llm_provider = require("llm_provider")


def _extract_tag_content(text: str, tag: str) -> str:
    """提取指定标签内的正文，避免把额外输出写入数据库。"""
    pattern = rf"<{tag}>([\s\S]*?)</{tag}>"
    match = re.search(pattern, text)
    if match:
        return re.sub(r"\s+", " ", match.group(1)).strip()

    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    if not lines:
        return ""

    if len(lines) > 1:
        logger.warning("[KomariMemory] 模糊化返回多行内容，降级使用首行")
    return re.sub(r"\s+", " ", lines[0]).strip()


def _is_invalid_fuzzy_summary(value: str) -> bool:
    """判断模糊化结果是否为空或已知占位文本。"""
    normalized = value.strip()
    return normalized in {
        "",
        "对话内容已模糊化处理",
        "互动事件已模糊化处理",
    }


def _safe_record_value(record: asyncpg.Record | dict[str, Any], key: str) -> Any:
    """安全读取 asyncpg.Record 或测试替身中的字段。"""
    try:
        return record[key]
    except (KeyError, TypeError):
        return None


def _safe_record_id_for_log(record: asyncpg.Record | dict[str, Any]) -> str:
    """生成用于日志的记录 ID，坏行也不影响批处理。"""
    value = _safe_record_value(record, "id")
    return "<missing>" if value is None else str(value)


def _parse_fuzzify_record(
    record: asyncpg.Record | dict[str, Any],
    summary_key: str,
    task_label: str,
) -> tuple[int, str] | None:
    """解析待模糊化记录，坏行返回 None 而不是抛出异常。"""
    raw_id = _safe_record_value(record, "id")
    raw_summary = _safe_record_value(record, summary_key)

    try:
        record_id = int(raw_id)
    except (TypeError, ValueError):
        logger.warning(
            "[KomariMemory] 跳过无效{}模糊化记录: id={}",
            task_label,
            raw_id,
        )
        return None

    if not isinstance(raw_summary, str):
        logger.warning(
            "[KomariMemory] 跳过无效{}模糊化记录: id={}, {} 不是字符串",
            task_label,
            record_id,
            summary_key,
        )
        return None

    return record_id, raw_summary


class ForgettingService:
    """记忆忘却服务。"""

    def __init__(
        self,
        pg_pool: asyncpg.Pool,
        *,
        config_provider: Callable[[], KomariMemoryConfigSchema] = get_config,
    ) -> None:
        """初始化忘却服务。

        Args:
            pg_pool: PostgreSQL连接池
            config_provider: 每次执行时读取当前配置的函数
        """
        self.pg_pool = pg_pool
        self._config_provider = config_provider

    @property
    def config(self) -> KomariMemoryConfigSchema:
        """获取当前动态配置，避免服务长期持有启动快照。"""
        return self._config_provider()

    async def decay_and_cleanup(self) -> None:
        """执行死神脚本（每天凌晨4点）。

        处理流程：
        1. 检查是否启用忘却
        2. 所有记忆重要性按整数退一
        3. 删除低价值记忆
        4. 高价值记忆第一次归零时模糊化并恢复重要性，第二次归零删除
        """
        config = self.config
        if not config.forgetting_enabled:
            logger.debug("[KomariMemory] 忘却功能未启用，跳过")
            return

        logger.info("[KomariMemory] 死神脚本开始执行...")

        try:
            # 1. 每日衰减：所有记忆重要性按整数退一
            await self._daily_decay()
            await self._daily_decay_interaction_events()

            # 2. 删除低价值记忆
            deleted_low = await self._delete_low_value_memories()
            deleted_low += await self._delete_low_value_interaction_events()

            # 3. 模糊化或删除高价值记忆
            processed_high = await self._fuzzify_and_cleanup_high_value_memories()
            processed_high += await self._fuzzify_and_cleanup_high_value_interaction_events()

            logger.info(
                f"[KomariMemory] 死神脚本完成: "
                f"删除低价值 {deleted_low} 条, "
                f"删除/模糊化高价值 {processed_high} 条"
            )
        except Exception:
            logger.exception("[KomariMemory] 死神脚本执行失败")

    async def _daily_decay(self) -> None:
        """每日衰减：所有记忆重要性按整数退一。"""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE komari_memory_conversations
                SET importance_current = GREATEST(importance_current - 1, 0)
                """,
            )
            logger.debug("[KomariMemory] 已按整数退一衰减所有记忆的重要性")

    async def _daily_decay_interaction_events(self) -> None:
        """每日衰减跨群互动事件记忆。"""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE komari_memory_interaction_history
                SET importance_current = GREATEST(importance_current - 1, 0)
                """,
            )
            logger.debug("[KomariMemory] 已衰减跨群互动事件记忆的重要性")

    async def _delete_low_value_memories(self) -> int:
        """删除重要性=0的低价值记忆（初始评分≤配置阈值）。

        Returns:
            删除的记录数
        """
        config = self.config
        threshold = config.forgetting_importance_threshold
        min_age_days = config.forgetting_min_age_days
        async with self.pg_pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM komari_memory_conversations
                WHERE importance_current = 0
                  AND importance_initial <= $1
                  AND created_at <= NOW() - ($2 * INTERVAL '1 day')
                """,
                threshold,
                min_age_days,
            )
            deleted = result.split()[-1] if result else "0"
            logger.debug(
                "[KomariMemory] 删除低价值记忆: {} 条 (阈值: {}, 最小保留天数: {})",
                deleted,
                threshold,
                min_age_days,
            )
            return int(deleted)

    async def _delete_low_value_interaction_events(self) -> int:
        """删除低价值跨群互动事件记忆。"""
        config = self.config
        threshold = config.forgetting_importance_threshold
        min_age_days = config.forgetting_min_age_days
        async with self.pg_pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM komari_memory_interaction_history
                WHERE importance_current = 0
                  AND importance_initial <= $1
                  AND created_at <= NOW() - ($2 * INTERVAL '1 day')
                """,
                threshold,
                min_age_days,
            )
        deleted = int(result.split()[-1]) if result else 0
        logger.debug("[KomariMemory] 删除低价值跨群互动事件: {} 条", deleted)
        return deleted

    async def _fuzzify_and_cleanup_high_value_memories(self) -> int:
        """处理重要性=0的高价值记忆（初始评分>配置阈值）。

        Returns:
            处理的记录数（删除+模糊化）
        """
        config = self.config
        threshold = config.forgetting_importance_threshold
        min_age_days = config.forgetting_min_age_days
        async with self.pg_pool.acquire() as conn:
            fuzzy_result = await conn.execute(
                """
                DELETE FROM komari_memory_conversations
                WHERE importance_current = 0
                  AND importance_initial > $1
                  AND is_fuzzy = TRUE
                  AND created_at <= NOW() - ($2 * INTERVAL '1 day')
                """,
                threshold,
                min_age_days,
            )
            deleted_fuzzy = int(fuzzy_result.split()[-1]) if fuzzy_result else 0

            rows = await conn.fetch(
                """
                SELECT id, summary
                FROM komari_memory_conversations
                WHERE importance_current = 0
                  AND importance_initial > $1
                  AND is_fuzzy = FALSE
                  AND created_at <= NOW() - ($2 * INTERVAL '1 day')
                """,
                threshold,
                min_age_days,
            )

        if not rows:
            logger.debug(
                "[KomariMemory] 高价值记忆处理: 删除 {} 条, 模糊化 0 条 (最小保留天数: {})",
                deleted_fuzzy,
                min_age_days,
            )
            return deleted_fuzzy

        concurrency = max(1, int(config.forgetting_fuzzify_concurrency))
        semaphore = asyncio.Semaphore(concurrency)

        async def _fuzzify_record(record: asyncpg.Record | dict[str, Any]) -> bool:
            parsed = _parse_fuzzify_record(record, "summary", "对话记忆")
            if parsed is None:
                return False

            record_id, summary = parsed
            async with semaphore:
                return await self._fuzzify_conversation(
                    record_id,
                    summary,
                )

        logger.debug(
            "[KomariMemory] 准备并发模糊化高价值记忆: {} 条 (并发上限: {})",
            len(rows),
            concurrency,
        )
        results = await asyncio.gather(
            *(_fuzzify_record(record) for record in rows),
            return_exceptions=True,
        )
        fuzzified_count = 0
        for record, result in zip(rows, results, strict=True):
            if isinstance(result, BaseException):
                logger.opt(exception=result).error(
                    "[KomariMemory] 对话记忆模糊化任务异常，跳过当前记录: ID={}",
                    _safe_record_id_for_log(record),
                )
            elif result:
                fuzzified_count += 1

        logger.debug(
            "[KomariMemory] 高价值记忆处理: 删除 {} 条, 模糊化 {} 条 (最小保留天数: {}, 并发上限: {})",
            deleted_fuzzy,
            fuzzified_count,
            min_age_days,
            concurrency,
        )
        return deleted_fuzzy + fuzzified_count

    async def _fuzzify_and_cleanup_high_value_interaction_events(self) -> int:
        """处理高价值跨群互动事件的模糊化或二次删除。"""
        config = self.config
        threshold = config.forgetting_importance_threshold
        min_age_days = config.forgetting_min_age_days
        async with self.pg_pool.acquire() as conn:
            fuzzy_result = await conn.execute(
                """
                DELETE FROM komari_memory_interaction_history
                WHERE importance_current = 0
                  AND importance_initial > $1
                  AND is_fuzzy = TRUE
                  AND created_at <= NOW() - ($2 * INTERVAL '1 day')
                """,
                threshold,
                min_age_days,
            )
            deleted_fuzzy = int(fuzzy_result.split()[-1]) if fuzzy_result else 0
            rows = await conn.fetch(
                """
                SELECT id, event_summary
                FROM komari_memory_interaction_history
                WHERE importance_current = 0
                  AND importance_initial > $1
                  AND is_fuzzy = FALSE
                  AND created_at <= NOW() - ($2 * INTERVAL '1 day')
                """,
                threshold,
                min_age_days,
            )

        if not rows:
            return deleted_fuzzy

        concurrency = max(1, int(config.forgetting_fuzzify_concurrency))
        semaphore = asyncio.Semaphore(concurrency)

        async def _fuzzify_record(record: asyncpg.Record | dict[str, Any]) -> bool:
            parsed = _parse_fuzzify_record(record, "event_summary", "跨群互动事件")
            if parsed is None:
                return False

            record_id, summary = parsed
            async with semaphore:
                return await self._fuzzify_interaction_event(
                    record_id,
                    summary,
                )

        results = await asyncio.gather(
            *(_fuzzify_record(record) for record in rows),
            return_exceptions=True,
        )
        fuzzified_count = 0
        for record, result in zip(rows, results, strict=True):
            if isinstance(result, BaseException):
                logger.opt(exception=result).error(
                    "[KomariMemory] 跨群互动事件模糊化任务异常，跳过当前记录: ID={}",
                    _safe_record_id_for_log(record),
                )
            elif result:
                fuzzified_count += 1
        logger.debug(
            "[KomariMemory] 高价值跨群互动事件处理: 删除 {} 条, 模糊化 {} 条",
            deleted_fuzzy,
            fuzzified_count,
        )
        return deleted_fuzzy + fuzzified_count

    async def _fuzzify_conversation(self, conv_id: int, original_summary: str) -> bool:
        """模糊化对话记忆并重置重要性。"""
        try:
            fuzzy_summary = await self._generate_fuzzy_summary(original_summary, conv_id)
        except Exception:
            logger.exception(
                "[KomariMemory] 模糊化重试失败，删除记忆且不写入占位文本: ID={}",
                conv_id,
            )
            return await self._delete_conversation_after_fuzzify_failure(conv_id)

        try:
            async with self.pg_pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE komari_memory_conversations
                    SET summary = $1, is_fuzzy = TRUE, importance_current = importance_initial
                    WHERE id = $2
                    """,
                    fuzzy_summary,
                    conv_id,
                )
                logger.debug("[KomariMemory] 模糊化记忆: ID={}", conv_id)
                return True
        except Exception:
            logger.exception("[KomariMemory] 模糊化记忆写入失败 ID={}", conv_id)
            return False

    @retry_async(max_attempts=3, base_delay=1.0)
    async def _generate_fuzzy_summary(self, original: str, conv_id: int) -> str:
        """生成模糊化总结，并只保留正文。"""
        config = self.config
        tag = (config.response_tag or "content").strip() or "content"
        prompt = (
            "请将下面的对话总结模糊化为一句简短的简体中文概要。\n"
            "要求：\n"
            "1. 只保留核心主题，删除具体细节、数量、时间、地点、称呼和原话。\n"
            "2. 输出必须是一句简短自然的简体中文，不要换行。\n"
            f"3. 最终只能输出 <{tag}>模糊化后的结果</{tag}>。\n"
            "4. 标签外不要输出任何解释、前缀、后缀、Markdown、代码块或引号。\n\n"
            f"原始总结：\n{original}"
        )

        response = await llm_provider.generate_text(
            prompt=prompt,
            model=config.llm_model_summary,
            temperature=config.llm_temperature_summary,
            max_tokens=min(config.llm_max_tokens_summary, 120),
            thinking_mode=config.llm_thinking_mode_summary,
            reasoning_effort=config.llm_reasoning_effort_summary,
            request_trace_id=f"memfuzzy-{conv_id}",
            request_phase="forgetting_fuzzify",
        )
        fuzzy_summary = _extract_tag_content(response, tag)
        if _is_invalid_fuzzy_summary(fuzzy_summary):
            msg = f"模糊化结果无效: ID={conv_id}"
            raise ValueError(msg)

        return fuzzy_summary

    async def _delete_conversation_after_fuzzify_failure(self, conv_id: int) -> bool:
        """模糊化重试失败后删除对话记忆。"""
        try:
            async with self.pg_pool.acquire() as conn:
                await conn.execute(
                    """
                    DELETE FROM komari_memory_conversations
                    WHERE id = $1
                    """,
                    conv_id,
                )
        except Exception:
            logger.exception("[KomariMemory] 模糊化失败后的记忆删除失败 ID={}", conv_id)
            return False

        logger.info("[KomariMemory] 模糊化重试失败，已删除记忆 ID={}", conv_id)
        return True

    async def _fuzzify_interaction_event(self, event_id: int, original_summary: str) -> bool:
        """模糊化跨群互动事件并重置重要性。"""
        try:
            fuzzy_summary = await self._generate_fuzzy_interaction_summary(
                original_summary,
                event_id,
            )
        except Exception:
            logger.exception(
                "[KomariMemory] 跨群互动事件模糊化重试失败，删除事件且不写入占位文本: ID={}",
                event_id,
            )
            return await self._delete_interaction_after_fuzzify_failure(event_id)

        try:
            async with self.pg_pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE komari_memory_interaction_history
                    SET event_summary = $1,
                        is_fuzzy = TRUE,
                        importance_current = importance_initial
                    WHERE id = $2
                    """,
                    fuzzy_summary,
                    event_id,
                )
        except Exception:
            logger.exception("[KomariMemory] 跨群互动事件模糊化写入失败 ID={}", event_id)
            return False
        else:
            logger.debug("[KomariMemory] 模糊化跨群互动事件: ID={}", event_id)
            return True

    @retry_async(max_attempts=3, base_delay=1.0)
    async def _generate_fuzzy_interaction_summary(self, original: str, event_id: int) -> str:
        """生成跨群互动事件模糊化总结。"""
        config = self.config
        tag = (config.response_tag or "content").strip() or "content"
        prompt = (
            "请将下面的小鞠与某用户的长期互动事件记忆模糊化为一句简短的简体中文概要。\n"
            "要求：\n"
            "1. 保留关系基调、用户偏好倾向和互动模式。\n"
            "2. 淡化具体消息、时间线、可识别细节，不引入群号或群名。\n"
            f"3. 最终只能输出 <{tag}>模糊化后的结果</{tag}>。\n"
            "4. 标签外不要输出任何解释。\n\n"
            f"原始事件：\n{original}"
        )
        response = await llm_provider.generate_text(
            prompt=prompt,
            model=config.llm_model_summary,
            temperature=config.llm_temperature_summary,
            max_tokens=min(config.llm_max_tokens_summary, 120),
            thinking_mode=config.llm_thinking_mode_summary,
            reasoning_effort=config.llm_reasoning_effort_summary,
            request_trace_id=f"memfuzzy-interaction-{event_id}",
            request_phase="forgetting_interaction_fuzzify",
        )
        fuzzy_summary = _extract_tag_content(response, tag)
        if _is_invalid_fuzzy_summary(fuzzy_summary):
            msg = f"跨群互动事件模糊化结果无效: ID={event_id}"
            raise ValueError(msg)

        return fuzzy_summary

    async def _delete_interaction_after_fuzzify_failure(self, event_id: int) -> bool:
        """模糊化重试失败后删除跨群互动事件。"""
        try:
            async with self.pg_pool.acquire() as conn:
                await conn.execute(
                    """
                    DELETE FROM komari_memory_interaction_history
                    WHERE id = $1
                    """,
                    event_id,
                )
        except Exception:
            logger.exception(
                "[KomariMemory] 跨群互动事件模糊化失败后的删除失败 ID={}",
                event_id,
            )
            return False

        logger.info("[KomariMemory] 跨群互动事件模糊化重试失败，已删除事件 ID={}", event_id)
        return True
