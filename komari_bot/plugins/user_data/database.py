"""User data PostgreSQL access layer."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nonebot import logger

from komari_bot.common.database_config import get_shared_database_config
from komari_bot.common.postgres import create_postgres_pool

from .models import (
    FavorabilityAdjustmentResult,
    FavorabilitySetResult,
    UserFavorability,
)

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
        logger.debug("[UserDataDB] 开始初始化 PostgreSQL 连接池")
        self._pool = await create_postgres_pool(db_config)
        await self._create_tables()
        logger.debug("[UserDataDB] PostgreSQL 连接池与表结构初始化完成")

    def _require_pool(self) -> "asyncpg.Pool":
        """获取已初始化的数据库连接池。"""
        if self._pool is None:
            msg = "UserDataDB 连接池未初始化"
            raise RuntimeError(msg)
        return self._pool

    async def _create_tables(self) -> None:
        """创建数据库表结构。"""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await self._rebuild_legacy_favorability_table(conn)
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_favorability (
                    user_id TEXT PRIMARY KEY,
                    favorability INTEGER NOT NULL DEFAULT 0
                        CHECK (favorability >= 0 AND favorability <= 400),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await conn.execute(
                f"""
                ALTER TABLE user_favorability
                ALTER COLUMN favorability SET DEFAULT {self.config.initial_favorability}
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

    async def get_user_favorability(self, user_id: str) -> UserFavorability:
        """获取用户当前好感度，无记录时创建初始值。"""
        pool = self._require_pool()
        async with pool.acquire() as conn:
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
        if self._pool is None:
            logger.error(
                "[UserDataDB] 好感度调整失败：连接池未初始化 user={} delta={}",
                user_id,
                delta,
            )
        pool = self._require_pool()

        logger.debug(
            "[UserDataDB] 开始调整好感度: user={} delta={} initial={}",
            user_id,
            delta,
            self.config.initial_favorability,
        )
        async with pool.acquire() as conn, conn.transaction():
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
            logger.debug(
                "[UserDataDB] 已锁定好感度行: user={} before={}",
                user_id,
                before,
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

        if row is None:
            logger.error(
                "[UserDataDB] 好感度 UPDATE 未返回记录: user={} before={} delta={}",
                user_id,
                before,
                delta,
            )
            msg = "好感度 UPDATE 未返回记录"
            raise RuntimeError(msg)

        after = int(row["favorability"])
        before_value = int(before or self.config.initial_favorability)
        logger.debug(
            "[UserDataDB] 好感度调整完成: user={} before={} delta={} after={} updated_at={}",
            row["user_id"],
            before_value,
            delta,
            after,
            row["updated_at"],
        )

        return FavorabilityAdjustmentResult.from_values(
            user_id=row["user_id"],
            before=before_value,
            delta=delta,
            after=after,
            updated_at=row["updated_at"].isoformat(),
        )

    async def set_user_favorability(
        self,
        user_id: str,
        value: int,
    ) -> FavorabilitySetResult:
        """原子设置用户当前好感度为绝对值。

        对新用户以配置中的 initial_favorability 作为 before；
        已有用户以当前实际值作为 before。通过行锁与 adjust 串行化。
        """
        if not 0 <= value <= 400:
            msg = f"好感度值 {value} 越界，需在 [0, 400] 范围内"
            raise ValueError(msg)

        pool = self._require_pool()

        logger.debug(
            "[UserDataDB] 开始设置好感度: user={} value={} initial={}",
            user_id,
            value,
            self.config.initial_favorability,
        )
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                INSERT INTO user_favorability (user_id, favorability)
                VALUES ($1, $2)
                ON CONFLICT (user_id) DO NOTHING
                """,
                user_id,
                self.config.initial_favorability,
            )
            before_row = await conn.fetchrow(
                """
                SELECT favorability
                FROM user_favorability
                WHERE user_id = $1
                FOR UPDATE
                """,
                user_id,
            )
            before = int(before_row["favorability"] if before_row else self.config.initial_favorability)
            logger.debug(
                "[UserDataDB] 已锁定好感度行: user={} before={}",
                user_id,
                before,
            )
            updated = await conn.fetchrow(
                """
                UPDATE user_favorability
                SET favorability = $2,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = $1
                RETURNING user_id, favorability, updated_at
                """,
                user_id,
                value,
            )

        if updated is None:
            logger.error(
                "[UserDataDB] 好感度 SET 未返回记录: user={} value={}",
                user_id,
                value,
            )
            msg = "好感度 SET 未返回记录"
            raise RuntimeError(msg)

        after = int(updated["favorability"])
        logger.debug(
            "[UserDataDB] 好感度设置完成: user={} before={} after={} updated_at={}",
            updated["user_id"],
            before,
            after,
            updated["updated_at"],
        )

        return FavorabilitySetResult.from_values(
            user_id=updated["user_id"],
            before=before,
            after=after,
            updated_at=updated["updated_at"].isoformat(),
        )

    async def get_user_count(self) -> int:
        """获取总用户数。"""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT COUNT(*) FROM user_favorability
                """
            )
        return int(value or 0)
