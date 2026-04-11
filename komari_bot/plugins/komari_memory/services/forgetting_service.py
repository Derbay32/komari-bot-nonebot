"""记忆忘却服务 - 定期清理和模糊化到期记忆。"""

import asyncio
import re

import asyncpg
from nonebot import logger
from nonebot.plugin import require

from ..config_schema import KomariMemoryConfigSchema

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


class ForgettingService:
    """记忆忘却服务。"""

    def __init__(
        self,
        config: KomariMemoryConfigSchema,
        pg_pool: asyncpg.Pool,
    ) -> None:
        """初始化忘却服务。

        Args:
            config: 插件配置
            pg_pool: PostgreSQL连接池
        """
        self.config = config
        self.pg_pool = pg_pool

    async def decay_and_cleanup(self) -> None:
        """执行死神脚本（每天凌晨4点）。

        处理流程：
        1. 检查是否启用忘却
        2. 所有记忆重要性按整数退一
        3. 删除低价值记忆
        4. 高价值记忆第一次归零时模糊化并恢复重要性，第二次归零删除
        """
        if not self.config.forgetting_enabled:
            logger.debug("[KomariMemory] 忘却功能未启用，跳过")
            return

        logger.info("[KomariMemory] 死神脚本开始执行...")

        try:
            # 1. 每日衰减：所有记忆重要性按整数退一
            await self._daily_decay()

            # 2. 删除低价值记忆
            deleted_low = await self._delete_low_value_memories()

            # 3. 模糊化或删除高价值记忆
            processed_high = await self._fuzzify_and_cleanup_high_value_memories()

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

    async def _delete_low_value_memories(self) -> int:
        """删除重要性=0的低价值记忆（初始评分≤配置阈值）。

        Returns:
            删除的记录数
        """
        threshold = self.config.forgetting_importance_threshold
        min_age_days = self.config.forgetting_min_age_days
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

    async def _fuzzify_and_cleanup_high_value_memories(self) -> int:
        """处理重要性=0的高价值记忆（初始评分>配置阈值）。

        Returns:
            处理的记录数（删除+模糊化）
        """
        threshold = self.config.forgetting_importance_threshold
        min_age_days = self.config.forgetting_min_age_days
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

        concurrency = max(1, int(self.config.forgetting_fuzzify_concurrency))
        semaphore = asyncio.Semaphore(concurrency)

        async def _fuzzify_record(record: asyncpg.Record) -> bool:
            async with semaphore:
                return await self._fuzzify_conversation(
                    int(record["id"]),
                    str(record["summary"]),
                )

        logger.debug(
            "[KomariMemory] 准备并发模糊化高价值记忆: {} 条 (并发上限: {})",
            len(rows),
            concurrency,
        )
        results = await asyncio.gather(
            *(_fuzzify_record(record) for record in rows),
            return_exceptions=False,
        )
        fuzzified_count = sum(1 for result in results if result)

        logger.debug(
            "[KomariMemory] 高价值记忆处理: 删除 {} 条, 模糊化 {} 条 (最小保留天数: {}, 并发上限: {})",
            deleted_fuzzy,
            fuzzified_count,
            min_age_days,
            concurrency,
        )
        return deleted_fuzzy + fuzzified_count

    async def _fuzzify_conversation(self, conv_id: int, original_summary: str) -> bool:
        """模糊化对话记忆并重置重要性。"""
        try:
            fuzzy_summary = await self._generate_fuzzy_summary(original_summary, conv_id)

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
            logger.exception("[KomariMemory] 模糊化失败 ID={}", conv_id)
            return False

    async def _generate_fuzzy_summary(self, original: str, conv_id: int) -> str:
        """生成模糊化总结，并只保留正文。"""
        tag = (self.config.response_tag or "content").strip() or "content"
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
            model=self.config.llm_model_summary,
            temperature=self.config.llm_temperature_summary,
            max_tokens=min(self.config.llm_max_tokens_summary, 120),
            request_trace_id=f"memfuzzy-{conv_id}",
            request_phase="forgetting_fuzzify",
        )
        fuzzy_summary = _extract_tag_content(response, tag)
        if fuzzy_summary:
            return fuzzy_summary

        logger.warning("[KomariMemory] 模糊化结果为空，使用默认占位文本: ID={}", conv_id)
        return "对话内容已模糊化处理"
