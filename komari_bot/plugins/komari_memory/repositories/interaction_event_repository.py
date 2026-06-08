"""跨群互动事件记忆仓库。"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from nonebot import logger

if TYPE_CHECKING:
    from datetime import datetime

    import asyncpg


def _build_content_hash(content: str) -> str:
    """生成稳定的文本内容哈希。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class InteractionEventRepository:
    """新 interaction history 事件向量表数据访问仓库。"""

    def __init__(self, pg_pool: asyncpg.Pool) -> None:
        self.pg_pool = pg_pool

    async def insert_interaction_event(
        self,
        *,
        user_id: str,
        display_name: str,
        event_summary: str,
        embedding: str,
        source_message_count: int,
        first_seen_at: datetime,
        last_seen_at: datetime,
        importance_initial: int,
    ) -> int:
        """插入一条总结后的跨群互动事件记忆。"""
        importance = max(1, min(5, int(importance_initial)))
        async with self.pg_pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO komari_memory_interaction_history (
                    user_id,
                    display_name,
                    event_summary,
                    source_message_count,
                    first_seen_at,
                    last_seen_at,
                    importance,
                    importance_initial,
                    importance_current
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $7, $7)
                RETURNING id
                """,
                user_id,
                display_name,
                event_summary,
                source_message_count,
                first_seen_at,
                last_seen_at,
                importance,
            )
            if row is not None:
                await self._upsert_embedding(conn, int(row["id"]), event_summary, embedding)
        if row is None:
            msg = "插入跨群互动事件记忆失败"
            raise RuntimeError(msg)
        event_id = int(row["id"])
        logger.info(
            "[KomariMemory] 写入跨群互动事件记忆: id={} user={} count={} importance={}",
            event_id,
            user_id,
            source_message_count,
            importance,
        )
        return event_id

    async def get_interaction_event(self, event_id: int) -> dict[str, Any] | None:
        """按 ID 读取事件记忆。"""
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    id,
                    user_id,
                    display_name,
                    event_summary,
                    source_message_count,
                    first_seen_at,
                    last_seen_at,
                    importance,
                    importance_initial,
                    importance_current,
                    last_accessed,
                    is_fuzzy,
                    created_at
                FROM komari_memory_interaction_history
                WHERE id = $1
                """,
                event_id,
            )
        return dict(row) if row is not None else None

    async def search_interaction_events(
        self,
        *,
        user_id: str,
        embedding: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """按用户固定过滤并向量检索事件记忆。"""
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    h.id,
                    h.user_id,
                    h.display_name,
                    h.event_summary,
                    h.source_message_count,
                    h.first_seen_at,
                    h.last_seen_at,
                    h.importance,
                    h.importance_initial,
                    h.importance_current,
                    h.last_accessed,
                    h.is_fuzzy,
                    h.created_at,
                    1 - (e.embedding <=> $2::vector) AS similarity
                FROM komari_memory_interaction_history h
                JOIN komari_memory_interaction_embeddings e ON e.interaction_id = h.id
                WHERE h.user_id = $1
                ORDER BY
                    e.embedding <=> $2::vector,
                    h.importance_current DESC,
                    h.last_seen_at DESC
                LIMIT $3
                """,
                user_id,
                embedding,
                limit,
            )
        return [dict(row) for row in rows]

    async def list_interaction_events(
        self,
        *,
        limit: int,
        offset: int,
        user_id: str | None = None,
        query: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """分页列出互动事件记忆。"""
        filters: list[str] = []
        params: list[object] = []
        if user_id:
            params.append(user_id)
            filters.append(f"user_id = ${len(params)}")
        if query:
            params.append(f"%{query}%")
            placeholder = len(params)
            filters.append(
                f"(user_id ILIKE ${placeholder} "
                f"OR display_name ILIKE ${placeholder} "
                f"OR event_summary ILIKE ${placeholder})"
            )
        where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""

        async with self.pg_pool.acquire() as conn:
            total = await conn.fetchval(
                f"""
                SELECT COUNT(*)
                FROM komari_memory_interaction_history
                {where_sql}
                """,
                *params,
            )
            rows = await conn.fetch(
                f"""
                SELECT
                    id,
                    user_id,
                    display_name,
                    event_summary,
                    source_message_count,
                    first_seen_at,
                    last_seen_at,
                    importance,
                    importance_initial,
                    importance_current,
                    last_accessed,
                    is_fuzzy,
                    created_at
                FROM komari_memory_interaction_history
                {where_sql}
                ORDER BY last_seen_at DESC, id DESC
                LIMIT ${len(params) + 1}
                OFFSET ${len(params) + 2}
                """,
                *params,
                limit,
                offset,
            )
        return [dict(row) for row in rows], int(total or 0)

    async def update_fuzzy_event(self, event_id: int, fuzzy_summary: str) -> None:
        """写入模糊化后的事件总结并恢复当前重要度。"""
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

    async def update_interaction_event(
        self,
        event_id: int,
        *,
        event_summary: str | None = None,
        embedding: str | None = None,
        importance_initial: int | None = None,
        importance_current: int | None = None,
    ) -> dict[str, Any] | None:
        """更新单条互动事件管理字段。"""
        updates: list[str] = []
        params: list[object] = [event_id]

        def _append(field_name: str, value: object | None) -> None:
            if value is None:
                return
            params.append(value)
            updates.append(f"{field_name} = ${len(params)}")

        _append("event_summary", event_summary)
        _append("importance_initial", importance_initial)
        _append("importance_current", importance_current)
        if not updates and embedding is None:
            return await self.get_interaction_event(event_id)

        async with self.pg_pool.acquire() as conn, conn.transaction():
            if updates:
                row = await conn.fetchrow(
                    f"""
                    UPDATE komari_memory_interaction_history
                    SET {", ".join(updates)}
                    WHERE id = $1
                    RETURNING
                        id,
                        user_id,
                        display_name,
                        event_summary,
                        source_message_count,
                        first_seen_at,
                        last_seen_at,
                        importance,
                        importance_initial,
                        importance_current,
                        last_accessed,
                        is_fuzzy,
                        created_at
                    """,
                    *params,
                )
            else:
                row = await conn.fetchrow(
                    """
                    SELECT
                        id,
                        user_id,
                        display_name,
                        event_summary,
                        source_message_count,
                        first_seen_at,
                        last_seen_at,
                        importance,
                        importance_initial,
                        importance_current,
                        last_accessed,
                        is_fuzzy,
                        created_at
                    FROM komari_memory_interaction_history
                    WHERE id = $1
                    """,
                    event_id,
                )
            if row is not None and embedding is not None:
                embedding_summary = (
                    event_summary if event_summary is not None else str(row["event_summary"])
                )
                await self._upsert_embedding(conn, event_id, embedding_summary, embedding)
        return dict(row) if row is not None else None

    async def touch_interaction_events(self, event_ids: list[int]) -> None:
        """刷新已召回事件的访问时间并恢复当前重要度。"""
        normalized_ids = [int(event_id) for event_id in event_ids if int(event_id) > 0]
        if not normalized_ids:
            return
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE komari_memory_interaction_history
                SET last_accessed = CURRENT_TIMESTAMP,
                    importance_current = importance_initial
                WHERE id = ANY($1::int[])
                """,
                normalized_ids,
            )

    async def delete_interaction_event(self, event_id: int) -> bool:
        """删除单条互动事件记忆。"""
        async with self.pg_pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM komari_memory_interaction_history
                WHERE id = $1
                """,
                event_id,
            )
        return result.endswith("1")

    async def _upsert_embedding(
        self,
        conn: Any,
        interaction_id: int,
        event_summary: str,
        embedding: str,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO komari_memory_interaction_embeddings
                (interaction_id, content_hash, embedding, embedding_dim)
            VALUES ($1, $2, $3, vector_dims($3::vector))
            ON CONFLICT (interaction_id) DO UPDATE SET
                content_hash = EXCLUDED.content_hash,
                embedding = EXCLUDED.embedding,
                embedding_dim = EXCLUDED.embedding_dim,
                embedded_at = CURRENT_TIMESTAMP
            """,
            interaction_id,
            _build_content_hash(event_summary),
            embedding,
        )
