"""记忆忘却服务 - 定期清理和压缩低价值记忆。"""

import asyncpg
from nonebot import logger
from nonebot.plugin import require

from ..config_schema import KomariMemoryConfigSchema

# 依赖 llm_provider 插件（用于模糊化）
llm_provider = require("llm_provider")


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
        2. 所有记忆重要性按系数衰减
        3. 处理重要性=0的记忆：
           - 低价值（初始≤阈值）：直接删除
           - 高价值（初始>阈值）且未模糊：模糊化并重置
           - 高价值且已模糊：删除
        """
        if not self.config.forgetting_enabled:
            logger.debug("[KomariMemory] 忘却功能未启用，跳过")
            return

        logger.info("[KomariMemory] 死神脚本开始执行...")

        try:
            # 1. 每日衰减：所有记忆重要性按系数衰减
            await self._daily_decay()

            # 2. 处理重要性=0的记忆
            deleted_low = await self._delete_low_value_memories()
            deleted_high = await self._fuzzify_and_cleanup_high_value_memories()

            logger.info(
                f"[KomariMemory] 死神脚本完成: "
                f"删除低价值 {deleted_low} 条, "
                f"删除/模糊化高价值 {deleted_high} 条"
            )
        except Exception:
            logger.exception("[KomariMemory] 死神脚本执行失败")

    async def _daily_decay(self) -> None:
        """每日衰减：所有记忆重要性按配置系数衰减。"""
        # 获取配置的衰减系数（转换为衰减量）
        # 例如：decay_factor=0.95 表示每次衰减5%，即 importance_current * 0.95
        # 但当前设计是整数衰减，所以暂时保持-1的简单逻辑
        # TODO: 改为使用 decay_factor 进行浮点数衰减
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE komari_memory_conversations SET importance_current = GREATEST(0, importance_current - 1)"
            )
            logger.debug(
                f"[KomariMemory] 已衰减所有记忆的重要性 (衰减系数: {self.config.forgetting_decay_factor})"
            )

    async def _delete_low_value_memories(self) -> int:
        """删除重要性=0的低价值记忆（初始评分≤配置阈值）。

        Returns:
            删除的记录数
        """
        threshold = self.config.forgetting_importance_threshold
        async with self.pg_pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM komari_memory_conversations WHERE importance_current = 0 AND importance_initial <= $1",
                threshold,
            )
            deleted = result.split()[-1] if result else "0"
            logger.debug(f"[KomariMemory] 删除低价值记忆: {deleted} 条 (阈值: {threshold})")
            return int(deleted)

    async def _fuzzify_and_cleanup_high_value_memories(self) -> int:
        """处理重要性=0的高价值记忆（初始评分>配置阈值）。

        Returns:
            处理的记录数（删除+模糊化）
        """
        threshold = self.config.forgetting_importance_threshold
        async with self.pg_pool.acquire() as conn:
            # 1. 删除已模糊化的高价值记忆
            fuzzy_result = await conn.execute(
                "DELETE FROM komari_memory_conversations WHERE importance_current = 0 AND importance_initial > $1 AND is_fuzzy = TRUE",
                threshold,
            )
            deleted_fuzzy = int(fuzzy_result.split()[-1]) if fuzzy_result else 0

            # 2. 模糊化未处理的高价值记忆
            rows = await conn.fetch(
                "SELECT id, summary FROM komari_memory_conversations WHERE importance_current = 0 AND importance_initial > $1 AND is_fuzzy = FALSE",
                threshold,
            )

            fuzzified_count = 0
            for record in rows:
                await self._fuzzify_conversation(record["id"], record["summary"])
                fuzzified_count += 1

            total = deleted_fuzzy + fuzzified_count
            logger.debug(
                f"[KomariMemory] 高价值记忆处理: 删除 {deleted_fuzzy} 条, 模糊化 {fuzzified_count} 条"
            )
            return total

    async def _fuzzify_conversation(self, conv_id: int, original_summary: str) -> None:
        """模糊化对话记忆并重置重要性。

        将详细总结压缩为概要，并重置重要性为初始值。

        Args:
            conv_id: 对话ID
            original_summary: 原始总结内容
        """
        try:
            # 调用LLM进行模糊化
            fuzzy_summary = await self._generate_fuzzy_summary(original_summary)

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
                logger.debug(f"[KomariMemory] 模糊化记忆: ID={conv_id}")
        except Exception:
            logger.exception(f"[KomariMemory] 模糊化失败 ID={conv_id}")

    async def _generate_fuzzy_summary(self, original: str) -> str:
        """生成模糊化总结。

        Args:
            original: 原始总结

        Returns:
            模糊化后的总结
        """
        prompt = f"""将以下对话总结压缩为一句话概要（保留主题，删除细节）：

{original}

只返回压缩后的一句话，不要有任何其他内容。"""

        try:
            response = await llm_provider.generate_text(
                prompt=prompt,
                provider=self.config.llm_provider,
                model=self.config.llm_model_summary,
                temperature=0.3,
                max_tokens=100,
            )
            return response.strip()
        except Exception:
            logger.warning("[KomariMemory] 模糊化生成失败", exc_info=True)
            return "对话内容已模糊化处理"
