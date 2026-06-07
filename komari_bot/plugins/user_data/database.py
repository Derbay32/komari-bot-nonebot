"""User data PostgreSQL access layer."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from komari_bot.common.database_config import get_shared_database_config
from komari_bot.common.postgres import create_postgres_pool

from .models import FavorabilityAdjustmentResult, UserAttribute, UserFavorability

if TYPE_CHECKING:
    import asyncpg

    from .config_schema import DynamicConfigSchema


class UserDataDB:
    """用户数据数据库操作类（PostgreSQL）。"""

    def __init__(self, config: "DynamicConfigSchema") -> None:
        self.config = config
        self._pool: asyncpg.Pool | None = None

    async def initialize(self) -> None:
        """初始化数据库连接和表结构。"""
        db_config = get_shared_database_config()
        self._pool = await create_postgres_pool(db_config)
        await self._create_tables()

    async def _create_tables(self) -> None:
        """创建数据库表结构。"""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_attributes (
                    id BIGSERIAL PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    attribute_name TEXT NOT NULL,
                    attribute_value TEXT,
                    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, attribute_name)
                )
                """
            )

            await self._rebuild_legacy_favorability_table(conn)
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_favorability (
                    user_id TEXT PRIMARY KEY,
                    favorability INTEGER NOT NULL DEFAULT 100
                        CHECK (favorability >= 0 AND favorability <= 400),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_user_attributes_composite
                ON user_attributes(user_id, attribute_name)
                """
            )

    async def _rebuild_legacy_favorability_table(self, conn: Any) -> None:
        """检测旧版每日好感表并破坏性重建。"""
        legacy_columns = await conn.fetch(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'user_favorability'
              AND column_name = ANY($1::text[])
            """,
            ["last_updated", "daily_favor", "cumulative_favor"],
        )
        if legacy_columns:
            await conn.execute("DROP TABLE IF EXISTS user_favorability")

    async def close(self) -> None:
        """关闭数据库连接池。"""
        if self._pool:
            await self._pool.close()
            self._pool = None

    async def get_user_attribute(self, user_id: str, attribute_name: str) -> str | None:
        """获取用户属性值。"""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT attribute_value
                FROM user_attributes
                WHERE user_id = $1 AND attribute_name = $2
                """,
                user_id,
                attribute_name,
            )
        return (
            str(row["attribute_value"])
            if row and row["attribute_value"] is not None
            else None
        )

    async def set_user_attribute(
        self,
        user_id: str,
        attribute_name: str,
        attribute_value: str,
    ) -> bool:
        """设置用户属性值。"""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO user_attributes
                (user_id, attribute_name, attribute_value, updated_at)
                VALUES ($1, $2, $3, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id, attribute_name)
                DO UPDATE SET
                    attribute_value = EXCLUDED.attribute_value,
                    updated_at = CURRENT_TIMESTAMP
                """,
                user_id,
                attribute_name,
                attribute_value,
            )
        return True

    async def get_user_attributes(self, user_id: str) -> list[UserAttribute]:
        """获取用户的所有属性。"""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT user_id, attribute_name, attribute_value, created_at, updated_at
                FROM user_attributes
                WHERE user_id = $1
                ORDER BY updated_at DESC
                """,
                user_id,
            )

        return [
            UserAttribute(
                user_id=row["user_id"],
                attribute_name=row["attribute_name"],
                attribute_value=row["attribute_value"],
                created_at=row["created_at"].isoformat() if row["created_at"] else None,
                updated_at=row["updated_at"].isoformat() if row["updated_at"] else None,
            )
            for row in rows
        ]

    async def get_user_favorability(self, user_id: str) -> UserFavorability:
        """获取用户当前好感度，无记录时创建初始值。"""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO user_favorability (user_id, favorability)
                VALUES ($1, $2)
                ON CONFLICT (user_id) DO UPDATE SET user_id = EXCLUDED.user_id
                RETURNING user_id, favorability, updated_at
                """,
                user_id,
                self.config.initial_favorability,
            )

        return UserFavorability.from_score(
            user_id=row["user_id"],
            favorability=row["favorability"],
            updated_at=row["updated_at"].isoformat(),
        )

    async def adjust_user_favorability(
        self,
        user_id: str,
        delta: int,
    ) -> FavorabilityAdjustmentResult:
        """原子调整用户当前好感度，并限制在 [0, 400]。"""
        assert self._pool is not None
        async with self._pool.acquire() as conn, conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO user_favorability (user_id, favorability)
                    VALUES ($1, $2)
                    ON CONFLICT (user_id) DO NOTHING
                    """,
                    user_id,
                    self.config.initial_favorability,
                )
                before = await conn.fetchval(
                    """
                    SELECT favorability
                    FROM user_favorability
                    WHERE user_id = $1
                    FOR UPDATE
                    """,
                    user_id,
                )
                row = await conn.fetchrow(
                    """
                    UPDATE user_favorability
                    SET favorability = LEAST(400, GREATEST(0, favorability + $2)),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = $1
                    RETURNING user_id, favorability, updated_at
                    """,
                    user_id,
                    delta,
                )

        return FavorabilityAdjustmentResult.from_values(
            user_id=row["user_id"],
            before=int(before or self.config.initial_favorability),
            delta=delta,
            after=row["after"],
            updated_at=row["updated_at"].isoformat(),
        )

    async def cleanup_old_attributes(self, retention_days: int = 30) -> bool:
        """清理长期未更新的用户属性。"""
        assert self._pool is not None
        if retention_days <= 0:
            return False

        cutoff_date = datetime.now().astimezone() - timedelta(days=retention_days)
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                DELETE FROM user_attributes
                WHERE updated_at < $1
                """,
                cutoff_date,
            )
        return True

    async def get_user_count(self) -> int:
        """获取总用户数。"""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT COUNT(*) FROM (
                    SELECT user_id FROM user_attributes
                    UNION
                    SELECT user_id FROM user_favorability
                ) AS users
                """
            )
        return int(value or 0)
