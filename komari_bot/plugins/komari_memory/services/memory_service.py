"""Komari Memory 记忆管理服务。"""

import asyncio
import time
from pathlib import Path
from typing import Any

import asyncpg
from fastembed import TextEmbedding
from nonebot import logger

from ..config_schema import KomariMemoryConfigSchema


class MemoryService:
    """记忆管理服务。"""

    def __init__(self, config: KomariMemoryConfigSchema, pg_pool: asyncpg.Pool) -> None:
        """初始化记忆服务。

        Args:
            config: 插件配置
            pg_pool: asyncpg 连接池
        """
        self.config = config
        self.pg_pool = pg_pool
        self._embed_model: TextEmbedding | None = None

    async def _get_embed_model(self) -> TextEmbedding:
        """延迟加载嵌入模型。

        Returns:
            TextEmbedding 实例
        """
        if self._embed_model is None:
            # 配置统一的缓存目录
            cache_dir = Path.home() / ".cache" / "komari_embeddings"
            cache_dir.mkdir(parents=True, exist_ok=True)

            # 在独立线程中加载模型，避免阻塞
            loop = asyncio.get_event_loop()
            self._embed_model = await loop.run_in_executor(
                None,
                lambda: TextEmbedding(
                    model_name=self.config.embedding_model,
                    cache_dir=str(cache_dir),
                ),
            )
            logger.info(
                f"[KomariMemory] 向量嵌入模型加载完成 (缓存: {cache_dir})"
            )
        assert self._embed_model is not None  # 为类型检查器确保非 None
        return self._embed_model

    async def _get_embedding(self, text: str) -> list[float]:
        """生成文本的向量嵌入。

        Args:
            text: 输入文本

        Returns:
            向量数组
        """
        embed_model = await self._get_embed_model()
        # fastembed 返回迭代器，转换为列表后取第一个
        loop = asyncio.get_event_loop()
        embeddings = await loop.run_in_executor(
            None, lambda: list(embed_model.embed([text]))
        )
        return embeddings[0].tolist()

    async def store_conversation(
        self,
        group_id: str,
        summary: str,
        participants: list[str],
    ) -> int:
        """存储对话总结（向量检索用 asyncpg）。

        Args:
            group_id: 群组 ID
            summary: 总结文本
            participants: 参与者列表

        Returns:
            创建的对话 ID
        """
        # 生成向量
        embedding = await self._get_embedding(summary)

        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO komari_memory_conversations
                (group_id, summary, embedding, participants, start_time, end_time)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
                """,
                group_id,
                summary,
                str(embedding),
                participants,
                time.time() - 3600,  # 假设持续 1 小时
                time.time(),
            )

            logger.info(
                f"[KomariMemory] 存储对话总结: ID={row['id']}, group={group_id}"
            )
            return row["id"]

    async def search_conversations(
        self,
        query: str,
        group_id: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """向量检索对话（asyncpg 原生 SQL）。

        Args:
            query: 查询文本
            group_id: 群组 ID
            limit: 返回数量限制

        Returns:
            检索结果列表，包含 summary, similarity 等
        """
        # 生成查询向量
        query_vec = await self._get_embedding(query)

        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, summary, participants,
                       1 - (embedding <=> $1::vector) as similarity
                FROM komari_memory_conversations
                WHERE group_id = $2
                ORDER BY embedding <=> $1::vector
                LIMIT $3
                """,
                str(query_vec),
                group_id,
                limit,
            )

            results = [dict(row) for row in rows]
            logger.debug(
                f"[KomariMemory] 检索对话: query='{query[:30]}...', "
                f"找到 {len(results)} 条结果"
            )
            return results

    async def upsert_entity(
        self,
        user_id: str,
        group_id: str,
        key: str,
        value: str,
        category: str,
        importance: int = 3,
    ) -> None:
        """创建或更新实体（使用 ORM）。

        Args:
            user_id: 用户 ID
            group_id: 群组 ID
            key: 实体键
            value: 实体值
            category: 分类
            importance: 重要性 (1-5)
        """

        # 检查是否存在
        async with self.pg_pool.acquire() as conn:
            existing = await conn.fetchrow(
                """
                SELECT id FROM komari_memory_entity
                WHERE user_id = $1 AND group_id = $2 AND key = $3
                """,
                user_id,
                group_id,
                key,
            )

            if existing:
                # 更新
                await conn.execute(
                    """
                    UPDATE komari_memory_entity
                    SET value = $1, category = $2, importance = $3
                    WHERE user_id = $4 AND group_id = $5 AND key = $6
                    """,
                    value,
                    category,
                    importance,
                    user_id,
                    group_id,
                    key,
                )
                logger.debug(f"[KomariMemory] 更新实体: {key}")
            else:
                # 创建
                await conn.execute(
                    """
                    INSERT INTO komari_memory_entity
                    (user_id, group_id, key, value, category, importance)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    user_id,
                    group_id,
                    key,
                    value,
                    category,
                    importance,
                )
                logger.debug(f"[KomariMemory] 创建实体: {key}")

    async def get_entities(
        self,
        user_id: str | None = None,
        group_id: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """获取实体列表。

        Args:
            user_id: 过滤用户 ID
            group_id: 过滤群组 ID
            limit: 返回数量限制

        Returns:
            实体列表
        """
        async with self.pg_pool.acquire() as conn:
            if user_id and group_id:
                rows = await conn.fetch(
                    """
                    SELECT user_id, group_id, key, value, category, importance
                    FROM komari_memory_entity
                    WHERE user_id = $1 AND group_id = $2
                    ORDER BY importance DESC
                    LIMIT $3
                    """,
                    user_id,
                    group_id,
                    limit,
                )
            elif group_id:
                rows = await conn.fetch(
                    """
                    SELECT user_id, group_id, key, value, category, importance
                    FROM komari_memory_entity
                    WHERE group_id = $1
                    ORDER BY importance DESC
                    LIMIT $2
                    """,
                    group_id,
                    limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT user_id, group_id, key, value, category, importance
                    FROM komari_memory_entity
                    ORDER BY importance DESC
                    LIMIT $1
                    """,
                    limit,
                )

            return [dict(row) for row in rows]
