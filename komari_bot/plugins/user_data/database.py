"""User data PostgreSQL access layer."""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from komari_bot.common.postgres import create_postgres_pool

from .models import FavorGenerationResult, UserAttribute, UserFavorability

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
        self._pool = await create_postgres_pool(self.config)
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

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_favorability (
                    user_id TEXT NOT NULL,
                    daily_favor INTEGER DEFAULT 0,
                    cumulative_favor INTEGER DEFAULT 0,
                    last_updated DATE NOT NULL,
                    PRIMARY KEY (user_id, last_updated)
                )
                """
            )

            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_user_attributes_composite
                ON user_attributes(user_id, attribute_name)
                """
            )

            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_user_favorability_composite
                ON user_favorability(user_id, last_updated)
                """
            )

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
        return str(row["attribute_value"]) if row and row["attribute_value"] is not None else None

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

        attributes: list[UserAttribute] = []
        for row in rows:
            created_at = row["created_at"].isoformat() if row["created_at"] else None
            updated_at = row["updated_at"].isoformat() if row["updated_at"] else None
            attributes.append(
                UserAttribute(
                    user_id=row["user_id"],
                    attribute_name=row["attribute_name"],
                    attribute_value=row["attribute_value"],
                    created_at=created_at,
                    updated_at=updated_at,
                )
            )
        return attributes

    async def get_user_favorability(
        self,
        user_id: str,
        target_date: date | None = None,
    ) -> UserFavorability | None:
        """获取用户好感度。"""
        assert self._pool is not None
        if target_date is None:
            target_date = datetime.now().astimezone().date()

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT user_id, daily_favor, cumulative_favor, last_updated
                FROM user_favorability
                WHERE user_id = $1 AND last_updated = $2
                """,
                user_id,
                target_date,
            )

        if not row:
            return None

        return UserFavorability(
            user_id=row["user_id"],
            daily_favor=row["daily_favor"],
            cumulative_favor=row["cumulative_favor"],
            last_updated=row["last_updated"],
        )

    async def generate_or_update_favorability(
        self,
        user_id: str,
    ) -> FavorGenerationResult:
        """生成或更新用户好感度。"""
        assert self._pool is not None
        today = datetime.now().astimezone().date()
        existing_favor = await self.get_user_favorability(user_id, today)

        is_new_day = False
        daily_favor = 0
        cumulative_favor = 0

        if existing_favor:
            daily_favor = existing_favor.daily_favor
            cumulative_favor = existing_favor.cumulative_favor
        else:
            is_new_day = True
            daily_favor = random.randint(1, 100)
            cumulative_favor = await self._get_cumulative_favor(user_id) + daily_favor

            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO user_favorability
                    (user_id, daily_favor, cumulative_favor, last_updated)
                    VALUES ($1, $2, $3, $4)
                    """,
                    user_id,
                    daily_favor,
                    cumulative_favor,
                    today,
                )

        favor_obj = UserFavorability(
            user_id=user_id,
            daily_favor=daily_favor,
            cumulative_favor=cumulative_favor,
            last_updated=today,
        )

        return FavorGenerationResult(
            user_id=user_id,
            daily_favor=daily_favor,
            cumulative_favor=cumulative_favor,
            is_new_day=is_new_day,
            favor_level=favor_obj.favor_level,
        )

    async def _get_cumulative_favor(self, user_id: str) -> int:
        """获取用户累计好感度（不包括今天）。"""
        assert self._pool is not None
        today = datetime.now().astimezone().date()
        async with self._pool.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT COALESCE(MAX(cumulative_favor), 0)
                FROM user_favorability
                WHERE user_id = $1 AND last_updated < $2
                """,
                user_id,
                today,
            )
        return int(value or 0)

    async def get_favor_history(
        self,
        user_id: str,
        days: int = 7,
    ) -> list[UserFavorability]:
        """获取用户好感度历史记录。"""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT user_id, daily_favor, cumulative_favor, last_updated
                FROM user_favorability
                WHERE user_id = $1
                ORDER BY last_updated DESC
                LIMIT $2
                """,
                user_id,
                days,
            )

        return [
            UserFavorability(
                user_id=row["user_id"],
                daily_favor=row["daily_favor"],
                cumulative_favor=row["cumulative_favor"],
                last_updated=row["last_updated"],
            )
            for row in rows
        ]

    async def cleanup_old_data(self, retention_days: int = 7) -> bool:
        """清理旧数据。"""
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
            await conn.execute(
                """
                DELETE FROM user_favorability
                WHERE last_updated < $1
                """,
                cutoff_date.date(),
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
