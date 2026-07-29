"""角色名绑定 PostgreSQL 访问层。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from nonebot import logger

from komari_bot.common.database_config import get_shared_database_config
from komari_bot.common.postgres import create_postgres_pool

if TYPE_CHECKING:
    import asyncpg


_SCHEMA_LOCK_KEY = "komari:character_binding:schema:v1"


class CharacterBindingDB:
    """角色名绑定数据库操作类。"""

    def __init__(self) -> None:
        self._pool: asyncpg.Pool | None = None
        self._initialize_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """初始化共享连接池租约与表结构。"""
        if self._pool is not None:
            return

        async with self._initialize_lock:
            if self._pool is not None:
                return

            pool = await create_postgres_pool(get_shared_database_config())
            try:
                await self._create_table(pool)
            except BaseException:
                try:
                    await pool.close()
                except Exception:
                    logger.exception(
                        "[CharacterBindingDB] 初始化失败后的连接池关闭失败"
                    )
                raise
            self._pool = pool

    @staticmethod
    async def _create_table(pool: asyncpg.Pool) -> None:
        """在事务级 advisory lock 下幂等创建绑定表。"""
        async with pool.acquire() as conn, conn.transaction():
            await conn.fetchval(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                _SCHEMA_LOCK_KEY,
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS komari_character_bindings (
                    user_id TEXT PRIMARY KEY,
                    character_name TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )

    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            msg = "character_binding 数据库尚未初始化"
            raise RuntimeError(msg)
        return self._pool

    async def load_all(self) -> dict[str, str]:
        """读取全部角色名绑定。"""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT user_id, character_name
                FROM komari_character_bindings
                ORDER BY user_id
                """
            )
        return {str(row["user_id"]): str(row["character_name"]) for row in rows}

    async def upsert(self, user_id: str, character_name: str) -> None:
        """新增或更新角色名绑定。"""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO komari_character_bindings (user_id, character_name)
                VALUES ($1, $2)
                ON CONFLICT (user_id) DO UPDATE
                SET character_name = EXCLUDED.character_name,
                    updated_at = now()
                """,
                user_id,
                character_name,
            )

    async def delete(self, user_id: str) -> bool:
        """删除角色名绑定，并返回记录是否存在。"""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM komari_character_bindings
                WHERE user_id = $1
                """,
                user_id,
            )
        return result == "DELETE 1"

    async def close(self) -> None:
        """释放当前实例持有的共享连接池租约。"""
        async with self._initialize_lock:
            pool = self._pool
            self._pool = None
            if pool is not None:
                await pool.close()


__all__ = ["CharacterBindingDB"]
