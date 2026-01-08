"""对话数据访问仓库。"""

import time
from typing import Any

import asyncpg
from nonebot import logger


class ConversationRepository:
    """对话数据访问仓库。"""

    def __init__(self, pg_pool: asyncpg.Pool) -> None:
        """初始化仓库。

        Args:
            pg_pool: PostgreSQL 连接池
        """
        self.pg_pool = pg_pool

    async def insert_conversation(
        self,
        group_id: str,
        summary: str,
        embedding: str,
        participants: list[str],
        importance_initial: int,
    ) -> int:
        """插入对话记录。

        Args:
            group_id: 群组 ID
            summary: 总结文本
            embedding: 向量嵌入（字符串格式）
            participants: 参与者列表
            importance_initial: 初始重要性评分

        Returns:
            创建的对话 ID
        """
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO komari_memory_conversations
                (group_id, summary, embedding, participants, start_time, end_time, importance_initial, importance_current)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING id
                """,
                group_id,
                summary,
                embedding,
                participants,
                time.time() - 3600,
                time.time(),
                importance_initial,
                importance_initial,
            )

            logger.info(
                f"[KomariMemory] 存储对话总结: ID={row['id']}, group={group_id}, importance={importance_initial}"
            )
            return row["id"]

    async def search_by_similarity(
        self,
        embedding: str,
        group_id: str,
        user_id: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """向量搜索对话（支持用户加权）。

        Args:
            embedding: 查询向量（字符串格式）
            group_id: 群组 ID
            user_id: 用户 ID（用于加权该用户参与的记忆）
            limit: 返回数量限制

        Returns:
            检索结果列表，包含 summary, similarity 等
        """
        async with self.pg_pool.acquire() as conn:
            if user_id:
                # 用户加权：使用 CASE WHEN 提升包含该用户 ID 的记忆
                rows = await conn.fetch(
                    """
                    SELECT
                        id, summary, participants,
                        1 - (embedding <=> $1::vector) as similarity
                    FROM komari_memory_conversations
                    WHERE group_id = $2
                    ORDER BY
                        CASE
                            WHEN $4 = ANY(participants) THEN
                                (embedding <=> $1::vector) / 1.2
                            ELSE
                                embedding <=> $1::vector
                        END
                    LIMIT $3
                    """,
                    embedding,
                    group_id,
                    limit,
                    user_id,
                )
            else:
                # 原有逻辑：无用户加权
                rows = await conn.fetch(
                    """
                    SELECT id, summary, participants,
                           1 - (embedding <=> $1::vector) as similarity
                    FROM komari_memory_conversations
                    WHERE group_id = $2
                    ORDER BY embedding <=> $1::vector
                    LIMIT $3
                    """,
                    embedding,
                    group_id,
                    limit,
                )

            results = [dict(row) for row in rows]

            # 检索后重置重要性和更新访问时间
            if results:
                result_ids = [r["id"] for r in results]
                await conn.execute(
                    """
                    UPDATE komari_memory_conversations
                    SET last_accessed = NOW(),
                        importance_current = importance_initial
                    WHERE id = ANY($1)
                    """,
                    result_ids,
                )
                logger.debug(
                    f"[KomariMemory] 重置 {len(result_ids)} 条记忆的重要性"
                )

            logger.debug(
                f"[KomariMemory] 检索对话: 找到 {len(results)} 条结果"
            )
            return results
