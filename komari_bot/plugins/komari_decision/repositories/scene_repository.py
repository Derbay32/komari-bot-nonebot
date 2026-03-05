"""Scene 持久化数据访问仓库。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from nonebot import logger

if TYPE_CHECKING:
    import asyncpg


class SceneRepository:
    """Scene 持久化数据访问仓库。"""

    def __init__(self, pg_pool: asyncpg.Pool) -> None:
        """初始化仓库。"""
        self.pg_pool = pg_pool
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()

    async def ensure_schema(self) -> None:
        """确保 scene 持久化相关表结构存在。"""
        if self._schema_ready:
            return

        async with self._schema_lock:
            if self._schema_ready:
                return

            async with self.pg_pool.acquire() as conn:
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS komari_memory_scene_set (
                        id BIGSERIAL PRIMARY KEY,
                        source_path TEXT NOT NULL,
                        source_hash TEXT NOT NULL,
                        embedding_model TEXT NOT NULL,
                        embedding_instruction_hash TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (status IN ('BUILDING', 'READY', 'FAILED')),
                        item_total INT NOT NULL DEFAULT 0 CHECK (item_total >= 0),
                        item_ready INT NOT NULL DEFAULT 0 CHECK (item_ready >= 0),
                        item_failed INT NOT NULL DEFAULT 0 CHECK (item_failed >= 0),
                        error_message TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        ready_at TIMESTAMPTZ
                    )
                    """
                )
                await conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_komari_memory_scene_set_status
                    ON komari_memory_scene_set(status, created_at DESC)
                    """
                )
                await conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_komari_memory_scene_set_source_hash
                    ON komari_memory_scene_set(source_hash)
                    """
                )
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS komari_memory_scene_item (
                        id BIGSERIAL PRIMARY KEY,
                        set_id BIGINT NOT NULL REFERENCES komari_memory_scene_set(id) ON DELETE CASCADE,
                        scene_key TEXT NOT NULL,
                        scene_type TEXT NOT NULL CHECK (scene_type IN ('fixed', 'general')),
                        content_text TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        enabled BOOLEAN NOT NULL DEFAULT TRUE,
                        order_index INT NOT NULL DEFAULT 0,
                        embedding REAL[],
                        embedding_dim INT,
                        status TEXT NOT NULL CHECK (status IN ('PENDING', 'READY', 'FAILED')),
                        error_message TEXT,
                        embedded_at TIMESTAMPTZ,
                        UNIQUE (set_id, scene_key)
                    )
                    """
                )
                await conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_komari_memory_scene_item_set_status
                    ON komari_memory_scene_item(set_id, status)
                    """
                )
                await conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_komari_memory_scene_item_reuse
                    ON komari_memory_scene_item(scene_key, content_hash)
                    """
                )
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS komari_memory_scene_runtime (
                        id INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
                        active_set_id BIGINT REFERENCES komari_memory_scene_set(id),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                await conn.execute(
                    """
                    INSERT INTO komari_memory_scene_runtime (id, active_set_id)
                    VALUES (1, NULL)
                    ON CONFLICT (id) DO NOTHING
                    """
                )

            self._schema_ready = True
            logger.info("[KomariDecision] scene 持久化表结构检查完成")

    async def create_scene_set(
        self,
        source_path: str,
        source_hash: str,
        embedding_model: str,
        embedding_instruction_hash: str,
        status: str = "BUILDING",
    ) -> int:
        """创建 scene set 版本记录。"""
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO komari_memory_scene_set
                (source_path, source_hash, embedding_model, embedding_instruction_hash, status)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
                """,
                source_path,
                source_hash,
                embedding_model,
                embedding_instruction_hash,
                status,
            )
            set_id = int(row["id"])
            logger.info(
                "[KomariDecision] 创建 scene set: id=%s status=%s model=%s",
                set_id,
                status,
                embedding_model,
            )
            return set_id

    async def insert_scene_items(
        self,
        set_id: int,
        items: list[dict[str, Any]],
    ) -> int:
        """批量插入 scene 条目。"""
        if not items:
            return 0

        values: list[tuple[Any, ...]] = [
            (
                set_id,
                str(item["scene_key"]),
                str(item["scene_type"]),
                str(item["content_text"]),
                str(item["content_hash"]),
                bool(item.get("enabled", True)),
                int(item.get("order_index", 0)),
                item.get("embedding"),
                item.get("embedding_dim"),
                str(item.get("status", "PENDING")),
                item.get("error_message"),
                item.get("embedded_at"),
            )
            for item in items
        ]
        async with self.pg_pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO komari_memory_scene_item
                (set_id, scene_key, scene_type, content_text, content_hash, enabled,
                 order_index, embedding, embedding_dim, status, error_message, embedded_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                """,
                values,
            )

            await conn.execute(
                """
                UPDATE komari_memory_scene_set
                SET item_total = item_total + $2
                WHERE id = $1
                """,
                set_id,
                len(values),
            )

        logger.info(
            "[KomariDecision] 批量插入 scene item: set=%s count=%s", set_id, len(values)
        )
        return len(values)

    async def get_scene_set(self, set_id: int) -> dict[str, Any] | None:
        """获取指定 scene set。"""
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, source_path, source_hash, embedding_model,
                       embedding_instruction_hash, status, item_total, item_ready,
                       item_failed, error_message, created_at, ready_at
                FROM komari_memory_scene_set
                WHERE id = $1
                """,
                set_id,
            )
            return dict(row) if row else None

    async def get_latest_ready_set(self) -> dict[str, Any] | None:
        """获取最新 READY scene set。"""
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, source_path, source_hash, embedding_model,
                       embedding_instruction_hash, status, item_total, item_ready,
                       item_failed, error_message, created_at, ready_at
                FROM komari_memory_scene_set
                WHERE status = 'READY'
                ORDER BY COALESCE(ready_at, created_at) DESC, id DESC
                LIMIT 1
                """
            )
            return dict(row) if row else None

    async def list_ready_sets(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """按时间倒序列出 READY scene set。"""
        sql = """
            SELECT id, source_path, source_hash, embedding_model,
                   embedding_instruction_hash, status, item_total, item_ready,
                   item_failed, error_message, created_at, ready_at
            FROM komari_memory_scene_set
            WHERE status = 'READY'
            ORDER BY COALESCE(ready_at, created_at) DESC, id DESC
        """
        params: list[Any] = []
        if limit is not None:
            sql += " LIMIT $1"
            params.append(limit)

        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
            return [dict(row) for row in rows]

    async def get_latest_set_by_fingerprint(
        self,
        source_hash: str,
        embedding_model: str,
        embedding_instruction_hash: str,
        *,
        status: str | None = None,
    ) -> dict[str, Any] | None:
        """按 fingerprint 获取最新 set，可选限定状态。"""
        sql = """
            SELECT id, source_path, source_hash, embedding_model,
                   embedding_instruction_hash, status, item_total, item_ready,
                   item_failed, error_message, created_at, ready_at
            FROM komari_memory_scene_set
            WHERE source_hash = $1
              AND embedding_model = $2
              AND embedding_instruction_hash = $3
        """
        params: list[Any] = [source_hash, embedding_model, embedding_instruction_hash]
        if status is not None:
            sql += " AND status = $4"
            params.append(status)
        sql += " ORDER BY created_at DESC, id DESC LIMIT 1"

        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(sql, *params)
            return dict(row) if row else None

    async def _ensure_runtime_row(self, conn: Any) -> None:
        """确保 runtime 指针行存在。"""
        await conn.execute(
            """
            INSERT INTO komari_memory_scene_runtime (id, active_set_id)
            VALUES (1, NULL)
            ON CONFLICT (id) DO NOTHING
            """
        )

    async def get_active_set(self) -> dict[str, Any] | None:
        """获取当前 active scene set。"""
        async with self.pg_pool.acquire() as conn:
            await self._ensure_runtime_row(conn)
            row = await conn.fetchrow(
                """
                SELECT s.id, s.source_path, s.source_hash, s.embedding_model,
                       s.embedding_instruction_hash, s.status, s.item_total, s.item_ready,
                       s.item_failed, s.error_message, s.created_at, s.ready_at,
                       r.updated_at AS runtime_updated_at
                FROM komari_memory_scene_runtime r
                LEFT JOIN komari_memory_scene_set s ON s.id = r.active_set_id
                WHERE r.id = 1
                """
            )
            if not row or row["id"] is None:
                return None
            return dict(row)

    async def set_active_set(self, set_id: int) -> None:
        """设置 active scene set 指针。"""
        async with self.pg_pool.acquire() as conn:
            await self._ensure_runtime_row(conn)
            await conn.execute(
                """
                UPDATE komari_memory_scene_runtime
                SET active_set_id = $1,
                    updated_at = NOW()
                WHERE id = 1
                """,
                set_id,
            )
        logger.info("[KomariDecision] 激活 scene set: id=%s", set_id)

    async def switch_active_set(self, set_id: int) -> None:
        """原子切换 active set（仅允许 READY 版本）。"""
        async with self.pg_pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT id, status
                FROM komari_memory_scene_set
                WHERE id = $1
                FOR UPDATE
                """,
                set_id,
            )
            if row is None:
                msg = f"scene set 不存在: {set_id}"
                raise ValueError(msg)
            status = str(row["status"])
            if status != "READY":
                msg = f"scene set 非 READY 状态，无法激活: id={set_id} status={status}"
                raise ValueError(msg)

            await self._ensure_runtime_row(conn)
            await conn.execute(
                """
                UPDATE komari_memory_scene_runtime
                SET active_set_id = $1,
                    updated_at = NOW()
                WHERE id = 1
                """,
                set_id,
            )
        logger.info("[KomariDecision] 原子切换 active scene set: id=%s", set_id)

    async def list_items_by_set(
        self,
        set_id: int,
        status: str | None = None,
        *,
        enabled_only: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """按 set 获取 scene 条目。"""
        sql = """
            SELECT id, set_id, scene_key, scene_type, content_text, content_hash,
                   enabled, order_index, embedding, embedding_dim,
                   status, error_message, embedded_at
            FROM komari_memory_scene_item
            WHERE set_id = $1
        """
        params: list[Any] = [set_id]
        idx = 2

        if status is not None:
            sql += f" AND status = ${idx}"
            params.append(status)
            idx += 1

        if enabled_only:
            sql += " AND enabled = TRUE"

        sql += " ORDER BY order_index ASC, id ASC"

        if limit is not None:
            sql += f" LIMIT ${idx}"
            params.append(limit)

        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
            return [dict(row) for row in rows]

    async def find_reusable_ready_item(
        self,
        scene_key: str,
        content_hash: str,
        embedding_model: str,
        embedding_instruction_hash: str,
    ) -> dict[str, Any] | None:
        """查找可复用 embedding 的 READY 条目。"""
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT i.id, i.set_id, i.scene_key, i.scene_type, i.content_text,
                       i.content_hash, i.enabled, i.order_index, i.embedding,
                       i.embedding_dim, i.status, i.error_message, i.embedded_at
                FROM komari_memory_scene_item i
                JOIN komari_memory_scene_set s ON s.id = i.set_id
                WHERE i.scene_key = $1
                  AND i.content_hash = $2
                  AND i.status = 'READY'
                  AND i.embedding IS NOT NULL
                  AND s.status = 'READY'
                  AND s.embedding_model = $3
                  AND s.embedding_instruction_hash = $4
                ORDER BY COALESCE(s.ready_at, s.created_at) DESC, s.id DESC
                LIMIT 1
                """,
                scene_key,
                content_hash,
                embedding_model,
                embedding_instruction_hash,
            )
            return dict(row) if row else None

    async def fetch_pending_items(
        self,
        set_id: int,
        *,
        limit: int = 32,
    ) -> list[dict[str, Any]]:
        """拉取待嵌入的 PENDING 条目。"""
        if limit <= 0:
            return []

        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, set_id, scene_key, scene_type, content_text, content_hash,
                       enabled, order_index, embedding, embedding_dim,
                       status, error_message, embedded_at
                FROM komari_memory_scene_item
                WHERE set_id = $1
                  AND status = 'PENDING'
                ORDER BY order_index ASC, id ASC
                LIMIT $2
                """,
                set_id,
                limit,
            )
            return [dict(row) for row in rows]

    async def mark_item_ready(
        self,
        item_id: int,
        embedding: list[float],
        embedding_dim: int,
    ) -> None:
        """将条目标记为 READY。"""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE komari_memory_scene_item
                SET embedding = $2,
                    embedding_dim = $3,
                    status = 'READY',
                    error_message = NULL,
                    embedded_at = NOW()
                WHERE id = $1
                """,
                item_id,
                embedding,
                embedding_dim,
            )

    async def mark_item_failed(self, item_id: int, error_message: str) -> None:
        """将条目标记为 FAILED。"""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE komari_memory_scene_item
                SET status = 'FAILED',
                    error_message = $2
                WHERE id = $1
                """,
                item_id,
                error_message,
            )

    async def update_set_counters(self, set_id: int) -> None:
        """基于 item 状态刷新 set 计数。"""
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE status = 'READY') AS ready_count,
                    COUNT(*) FILTER (WHERE status = 'FAILED') AS failed_count
                FROM komari_memory_scene_item
                WHERE set_id = $1
                """,
                set_id,
            )
            await conn.execute(
                """
                UPDATE komari_memory_scene_set
                SET item_total = $2,
                    item_ready = $3,
                    item_failed = $4
                WHERE id = $1
                """,
                set_id,
                int(row["total"]),
                int(row["ready_count"]),
                int(row["failed_count"]),
            )

    async def mark_set_ready(self, set_id: int) -> None:
        """将 set 标记为 READY。"""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE komari_memory_scene_set
                SET status = 'READY',
                    ready_at = NOW(),
                    error_message = NULL
                WHERE id = $1
                """,
                set_id,
            )
        logger.info("[KomariDecision] scene set 就绪: id=%s", set_id)

    async def mark_set_failed(self, set_id: int, error_message: str) -> None:
        """将 set 标记为 FAILED。"""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE komari_memory_scene_set
                SET status = 'FAILED',
                    error_message = $2
                WHERE id = $1
                """,
                set_id,
                error_message,
            )
        logger.warning("[KomariDecision] scene set 失败: id=%s error=%s", set_id, error_message)

    async def reopen_failed_set(self, set_id: int) -> int:
        """将 FAILED set 重置为 BUILDING，并将 FAILED item 置回 PENDING。"""
        async with self.pg_pool.acquire() as conn, conn.transaction():
            set_row = await conn.fetchrow(
                """
                SELECT status
                FROM komari_memory_scene_set
                WHERE id = $1
                FOR UPDATE
                """,
                set_id,
            )
            if set_row is None:
                msg = f"scene set 不存在: {set_id}"
                raise ValueError(msg)
            if str(set_row["status"]) != "FAILED":
                msg = f"仅允许重试 FAILED set: id={set_id} status={set_row['status']}"
                raise ValueError(msg)

            await conn.execute(
                """
                UPDATE komari_memory_scene_set
                SET status = 'BUILDING',
                    error_message = NULL,
                    ready_at = NULL
                WHERE id = $1
                """,
                set_id,
            )

            result = await conn.execute(
                """
                UPDATE komari_memory_scene_item
                SET status = 'PENDING',
                    error_message = NULL
                WHERE set_id = $1
                  AND status = 'FAILED'
                """,
                set_id,
            )
            updated = int(result.split()[-1])

        logger.info(
            "[KomariDecision] 重试 scene set: id=%s reset_failed_items=%s",
            set_id,
            updated,
        )
        return updated

    async def delete_set(self, set_id: int) -> bool:
        """删除指定 set（级联删除 item）。"""
        async with self.pg_pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM komari_memory_scene_set
                WHERE id = $1
                """,
                set_id,
            )
        affected = int(result.split()[-1])
        if affected > 0:
            logger.info("[KomariDecision] 删除 scene set: id=%s", set_id)
            return True
        return False
