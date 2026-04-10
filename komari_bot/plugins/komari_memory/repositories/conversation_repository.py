"""对话数据访问仓库。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from nonebot import logger

if TYPE_CHECKING:
    import asyncpg


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
                datetime.now() - timedelta(hours=1),  # noqa: DTZ005
                datetime.now(),  # noqa: DTZ005
                importance_initial,
                importance_initial,
            )

            logger.info(
                f"[KomariMemory] 存储对话总结: ID={row['id']}, group={group_id}, importance={importance_initial}"
            )
            return row["id"]

    async def list_conversations(
        self,
        *,
        limit: int,
        offset: int,
        group_id: str | None = None,
        participant: str | None = None,
        query: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """分页获取对话记忆列表。"""
        filters: list[str] = []
        params: list[object] = []

        if group_id:
            filters.append(f"group_id = ${len(params) + 1}")
            params.append(group_id)
        if participant:
            filters.append(f"${len(params) + 1} = ANY(participants)")
            params.append(participant)
        if query:
            filters.append(f"summary ILIKE ${len(params) + 1}")
            params.append(f"%{query}%")

        where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""

        async with self.pg_pool.acquire() as conn:
            total = await conn.fetchval(
                f"""
                SELECT COUNT(*)
                FROM komari_memory_conversations
                {where_sql}
                """,
                *params,
            )
            rows = await conn.fetch(
                f"""
                SELECT
                    id,
                    group_id,
                    summary,
                    participants,
                    start_time,
                    end_time,
                    importance_initial,
                    importance_current,
                    last_accessed,
                    created_at
                FROM komari_memory_conversations
                {where_sql}
                ORDER BY created_at DESC, id DESC
                LIMIT ${len(params) + 1}
                OFFSET ${len(params) + 2}
                """,
                *params,
                limit,
                offset,
            )

        return [dict(row) for row in rows], int(total or 0)

    async def get_conversation(self, conversation_id: int) -> dict[str, Any] | None:
        """按 ID 获取单条对话记忆。"""
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    id,
                    group_id,
                    summary,
                    participants,
                    start_time,
                    end_time,
                    importance_initial,
                    importance_current,
                    last_accessed,
                    created_at
                FROM komari_memory_conversations
                WHERE id = $1
                """,
                conversation_id,
            )
        return dict(row) if row is not None else None

    async def create_conversation(
        self,
        *,
        group_id: str,
        summary: str,
        embedding: str,
        participants: list[str],
        start_time: datetime,
        end_time: datetime,
        importance_initial: int,
        importance_current: float,
        last_accessed: datetime | None = None,
    ) -> dict[str, Any]:
        """创建可管理的对话记忆记录。"""
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO komari_memory_conversations
                (
                    group_id,
                    summary,
                    embedding,
                    participants,
                    start_time,
                    end_time,
                    importance_initial,
                    importance_current,
                    last_accessed
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, COALESCE($9, $6))
                RETURNING
                    id,
                    group_id,
                    summary,
                    participants,
                    start_time,
                    end_time,
                    importance_initial,
                    importance_current,
                    last_accessed,
                    created_at
                """,
                group_id,
                summary,
                embedding,
                participants,
                start_time,
                end_time,
                importance_initial,
                importance_current,
                last_accessed,
            )

        if row is None:
            msg = "创建对话记忆失败"
            raise RuntimeError(msg)
        return dict(row)

    async def update_conversation(
        self,
        conversation_id: int,
        *,
        group_id: str | None = None,
        summary: str | None = None,
        embedding: str | None = None,
        participants: list[str] | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        importance_initial: int | None = None,
        importance_current: float | None = None,
        last_accessed: datetime | None = None,
    ) -> dict[str, Any] | None:
        """更新单条对话记忆。"""
        updates: list[str] = []
        params: list[object] = [conversation_id]

        def _append_update(field_name: str, value: object | None) -> None:
            if value is None:
                return
            params.append(value)
            updates.append(f"{field_name} = ${len(params)}")

        _append_update("group_id", group_id)
        _append_update("summary", summary)
        _append_update("embedding", embedding)
        _append_update("participants", participants)
        _append_update("start_time", start_time)
        _append_update("end_time", end_time)
        _append_update("importance_initial", importance_initial)
        _append_update("importance_current", importance_current)
        _append_update("last_accessed", last_accessed)

        if not updates:
            return await self.get_conversation(conversation_id)

        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE komari_memory_conversations
                SET {', '.join(updates)}
                WHERE id = $1
                RETURNING
                    id,
                    group_id,
                    summary,
                    participants,
                    start_time,
                    end_time,
                    importance_initial,
                    importance_current,
                    last_accessed,
                    created_at
                """,
                *params,
            )

        return dict(row) if row is not None else None

    async def delete_conversation(self, conversation_id: int) -> bool:
        """删除单条对话记忆。"""
        async with self.pg_pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM komari_memory_conversations
                WHERE id = $1
                """,
                conversation_id,
            )
        return result.endswith("1")

    async def search_by_similarity(
        self,
        embedding: str,
        group_id: str,
        user_id: str | None = None,
        limit: int = 10,
        access_boost: float = 1.0,
        *,
        touch_results: bool = True,
    ) -> list[dict[str, Any]]:
        """向量搜索对话（支持用户加权）。

        Args:
            embedding: 查询向量（字符串格式）
            group_id: 群组 ID
            user_id: 用户 ID（用于加权该用户参与的记忆）
            limit: 返回数量限制
            access_boost: 命中后重要性回升系数
            touch_results: 是否更新命中结果的访问时间和重要性

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
            if results and touch_results:
                result_ids = [r["id"] for r in results]
                await self.touch_conversations(result_ids, access_boost=access_boost)

            logger.debug(f"[KomariMemory] 检索对话: 找到 {len(results)} 条结果")
            return results

    async def touch_conversations(
        self,
        conversation_ids: list[int],
        *,
        access_boost: float = 1.0,
    ) -> None:
        """更新命中对话的访问时间和重要性。"""
        if not conversation_ids:
            return

        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE komari_memory_conversations
                SET last_accessed = NOW(),
                    importance_current = LEAST(
                        5.0,
                        GREATEST(
                            importance_initial::DOUBLE PRECISION,
                            ROUND((importance_current * $2)::numeric, 3)::DOUBLE PRECISION
                        )
                    )
                WHERE id = ANY($1)
                """,
                conversation_ids,
                access_boost,
            )

        logger.debug("[KomariMemory] 更新 {} 条记忆的访问状态", len(conversation_ids))
