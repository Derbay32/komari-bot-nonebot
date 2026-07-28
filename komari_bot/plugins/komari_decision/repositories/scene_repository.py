"""Scene 持久化数据访问仓库。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import TYPE_CHECKING, Any

from nonebot import logger

from .scene_schema import SCENE_SCHEMA_STATEMENTS

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
                for statement in SCENE_SCHEMA_STATEMENTS:
                    await conn.execute(statement)

            self._schema_ready = True
            logger.info("[KomariDecision] scene 持久化表结构检查完成")

    @staticmethod
    def compute_text_hash(text: str) -> str:
        """计算 scene 内容哈希。"""
        return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()

    @staticmethod
    def compute_scene_source_hash(scenes: list[dict[str, Any]]) -> str:
        """基于启用 scene 内容计算规范化来源哈希。"""
        payload = {
            "scenes": [
                {
                    "scene_key": str(scene["scene_key"]),
                    "scene_type": str(scene["scene_type"]),
                    "content_hash": str(scene["content_hash"]),
                    "enabled": bool(scene.get("enabled", True)),
                    "order_index": int(scene.get("order_index", 0)),
                }
                for scene in scenes
            ]
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    async def list_scenes(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        """列出 scene 内容表记录。"""
        sql = """
            SELECT id, scene_key, scene_type, content_text, content_hash,
                   enabled, order_index, created_at, updated_at
            FROM komari_decision_scenes
        """
        if enabled_only:
            sql += " WHERE enabled = TRUE"
        sql += " ORDER BY order_index ASC, id ASC"
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch(sql)
            return [dict(row) for row in rows]

    async def get_scene_by_key(self, scene_key: str) -> dict[str, Any] | None:
        """按 scene_key 获取 scene 内容记录。"""
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, scene_key, scene_type, content_text, content_hash,
                       enabled, order_index, created_at, updated_at
                FROM komari_decision_scenes
                WHERE scene_key = $1
                """,
                scene_key,
            )
            return dict(row) if row else None

    async def upsert_scene(
        self,
        *,
        scene_key: str,
        scene_type: str,
        content_text: str,
        enabled: bool = True,
        order_index: int = 0,
    ) -> dict[str, Any]:
        """新增或更新 scene 内容记录。"""
        scene_key = scene_key.strip()
        scene_type = scene_type.strip()
        content_text = content_text.strip()
        if not scene_key:
            msg = "scene_key 不能为空"
            raise ValueError(msg)
        if scene_type not in {"fixed", "general"}:
            msg = "scene_type 只能是 fixed 或 general"
            raise ValueError(msg)
        if not content_text:
            msg = "content_text 不能为空"
            raise ValueError(msg)
        content_hash = self.compute_text_hash(content_text)
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO komari_decision_scenes
                (scene_key, scene_type, content_text, content_hash, enabled, order_index)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (scene_key) DO UPDATE
                SET scene_type = EXCLUDED.scene_type,
                    content_text = EXCLUDED.content_text,
                    content_hash = EXCLUDED.content_hash,
                    enabled = EXCLUDED.enabled,
                    order_index = EXCLUDED.order_index,
                    updated_at = NOW()
                RETURNING id, scene_key, scene_type, content_text, content_hash,
                          enabled, order_index, created_at, updated_at
                """,
                scene_key,
                scene_type,
                content_text,
                content_hash,
                enabled,
                order_index,
            )
            return dict(row)

    async def delete_scene(self, scene_key: str) -> bool:
        """删除 scene 内容记录，必需 fixed key 不允许删除。"""
        if scene_key in {"NOISE", "MEANINGFUL", "CALL_DIRECT", "CALL_MENTION"}:
            msg = f"必需 fixed scene 不允许删除: {scene_key}"
            raise ValueError(msg)
        async with self.pg_pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM komari_decision_scenes
                WHERE scene_key = $1
                """,
                scene_key,
            )
        affected = int(result.split()[-1])
        return affected > 0

    async def has_any_scene(self) -> bool:
        """检查 scene 内容表是否已有记录。"""
        async with self.pg_pool.acquire() as conn:
            value = await conn.fetchval("SELECT EXISTS (SELECT 1 FROM komari_decision_scenes)")
            return bool(value)

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
                "[KomariDecision] 创建 scene set: id={} status={} model={}",
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

        values: list[tuple[Any, ...]] = []
        for item in items:
            scene_id = item.get("scene_id")
            if scene_id is None:
                scene = await self.get_scene_by_key(str(item["scene_key"]))
                if scene is None:
                    msg = f"scene 内容记录不存在: {item['scene_key']}"
                    raise ValueError(msg)
                scene_id = scene["id"]
            values.append(
                (
                    set_id,
                    int(scene_id),
                    str(item["content_hash"]),
                    item.get("embedding"),
                    item.get("embedding_dim"),
                    str(item.get("status", "PENDING")),
                    item.get("error_message"),
                    item.get("embedded_at"),
                )
            )
        async with self.pg_pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO komari_memory_scene_item
                (set_id, scene_id, content_hash, embedding, embedding_dim,
                 status, error_message, embedded_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
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
            "[KomariDecision] 批量插入 scene item: set={} count={}",
            set_id,
            len(values),
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
        logger.info("[KomariDecision] 激活 scene set: id={}", set_id)

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
        logger.info("[KomariDecision] 原子切换 active scene set: id={}", set_id)

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
            SELECT i.id, i.set_id, i.scene_id, s.scene_key, s.scene_type,
                   s.content_text, i.content_hash, s.enabled, s.order_index,
                   i.embedding, i.embedding_dim, i.status, i.error_message, i.embedded_at
            FROM komari_memory_scene_item i
            JOIN komari_decision_scenes s ON s.id = i.scene_id
            WHERE i.set_id = $1
        """
        params: list[Any] = [set_id]
        idx = 2

        if status is not None:
            sql += f" AND i.status = ${idx}"
            params.append(status)
            idx += 1

        if enabled_only:
            sql += " AND s.enabled = TRUE"

        sql += " ORDER BY s.order_index ASC, i.id ASC"

        if limit is not None:
            sql += f" LIMIT ${idx}"
            params.append(limit)

        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
            return [dict(row) for row in rows]

    async def find_reusable_ready_item(
        self,
        *,
        scene_id: int | None = None,
        content_hash: str,
        embedding_model: str,
        embedding_instruction_hash: str,
        scene_key: str | None = None,
    ) -> dict[str, Any] | None:
        """查找可复用 embedding 的 READY 条目。"""
        if scene_id is None:
            if scene_key is None:
                msg = "scene_id 或 scene_key 必须提供一个"
                raise ValueError(msg)
            scene = await self.get_scene_by_key(scene_key)
            if scene is None:
                return None
            scene_id = int(scene["id"])
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT i.id, i.set_id, i.scene_id, ds.scene_key, ds.scene_type,
                       ds.content_text, i.content_hash, ds.enabled, ds.order_index,
                       i.embedding, i.embedding_dim, i.status, i.error_message, i.embedded_at
                FROM komari_memory_scene_item i
                JOIN komari_memory_scene_set s ON s.id = i.set_id
                JOIN komari_decision_scenes ds ON ds.id = i.scene_id
                WHERE i.scene_id = $1
                  AND i.content_hash = $2
                  AND i.status = 'READY'
                  AND i.embedding IS NOT NULL
                  AND s.status = 'READY'
                  AND s.embedding_model = $3
                  AND s.embedding_instruction_hash = $4
                ORDER BY COALESCE(s.ready_at, s.created_at) DESC, s.id DESC
                LIMIT 1
                """,
                scene_id,
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
                SELECT i.id, i.set_id, i.scene_id, s.scene_key, s.scene_type,
                       s.content_text, i.content_hash, s.enabled, s.order_index,
                       i.embedding, i.embedding_dim, i.status, i.error_message, i.embedded_at
                FROM komari_memory_scene_item i
                JOIN komari_decision_scenes s ON s.id = i.scene_id
                WHERE i.set_id = $1
                  AND i.status = 'PENDING'
                ORDER BY s.order_index ASC, i.id ASC
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
        logger.info("[KomariDecision] scene set 就绪: id={}", set_id)

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
        logger.warning(
            "[KomariDecision] scene set 失败: id={} error={}",
            set_id,
            error_message,
        )

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
            "[KomariDecision] 重试 scene set: id={} reset_failed_items={}",
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
            logger.info("[KomariDecision] 删除 scene set: id={}", set_id)
            return True
        return False
