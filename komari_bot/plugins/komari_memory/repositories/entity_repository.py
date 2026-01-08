"""实体数据访问仓库。"""

from typing import Any

import asyncpg
from nonebot import logger


class EntityRepository:
    """实体数据访问仓库。"""

    def __init__(self, pg_pool: asyncpg.Pool) -> None:
        """初始化仓库。

        Args:
            pg_pool: PostgreSQL 连接池
        """
        self.pg_pool = pg_pool

    async def upsert(
        self,
        user_id: str,
        group_id: str,
        key: str,
        value: str,
        category: str,
        importance: int,
    ) -> None:
        """创建或更新实体。

        Args:
            user_id: 用户 ID
            group_id: 群组 ID
            key: 实体键
            value: 实体值
            category: 分类
            importance: 重要性 (1-5)
        """
        async with self.pg_pool.acquire() as conn:
            # 检查是否存在
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

    async def list(
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
