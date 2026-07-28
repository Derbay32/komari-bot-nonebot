"""实体数据访问仓库（每用户两行 JSON 模型）。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from nonebot import logger

if TYPE_CHECKING:
    import asyncpg

_PROFILE_KEY = "user_profile"
_PROFILE_CATEGORY = "profile_json"
_INTERACTION_KEY = "interaction_history"
_INTERACTION_CATEGORY = "interaction_history"


class EntityRepository:
    """实体数据访问仓库。"""

    def __init__(self, pg_pool: asyncpg.Pool) -> None:
        self.pg_pool = pg_pool

    async def upsert_user_profile(
        self,
        *,
        user_id: str,
        group_id: str,
        profile: dict[str, Any],
        importance: int = 4,
    ) -> None:
        """写入用户画像 JSON。"""
        await self._upsert_json_row(
            user_id=user_id,
            group_id=group_id,
            key=_PROFILE_KEY,
            category=_PROFILE_CATEGORY,
            payload=profile,
            importance=importance,
        )

    async def upsert_interaction_history(
        self,
        *,
        user_id: str,
        group_id: str,
        interaction: dict[str, Any],
        importance: int = 5,
    ) -> None:
        """写入互动历史 JSON。"""
        await self._upsert_json_row(
            user_id=user_id,
            group_id=group_id,
            key=_INTERACTION_KEY,
            category=_INTERACTION_CATEGORY,
            payload=interaction,
            importance=importance,
        )

    async def get_user_profile(
        self,
        *,
        user_id: str,
        group_id: str,
    ) -> dict[str, Any] | None:
        """读取用户画像 JSON。"""
        return await self._get_json_row(
            user_id=user_id,
            group_id=group_id,
            key=_PROFILE_KEY,
        )

    async def get_interaction_history(
        self,
        *,
        user_id: str,
        group_id: str,
    ) -> dict[str, Any] | None:
        """读取互动历史 JSON。"""
        return await self._get_json_row(
            user_id=user_id,
            group_id=group_id,
            key=_INTERACTION_KEY,
        )

    async def _upsert_json_row(
        self,
        *,
        user_id: str,
        group_id: str,
        key: str,
        category: str,
        payload: dict[str, Any],
        importance: int,
    ) -> None:
        value = json.dumps(payload, ensure_ascii=False)
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO komari_memory_entity (user_id, group_id, key, value, category, importance)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (user_id, group_id, key)
                DO UPDATE SET
                    value = EXCLUDED.value,
                    category = EXCLUDED.category,
                    importance = EXCLUDED.importance
                """,
                user_id,
                group_id,
                key,
                value,
                category,
                importance,
            )
        logger.debug(
            "[KomariMemory] upsert entity row: group={} user={} key={}",
            group_id,
            user_id,
            key,
        )

    async def _get_json_row(
        self,
        *,
        user_id: str,
        group_id: str,
        key: str,
    ) -> dict[str, Any] | None:
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT value
                FROM komari_memory_entity
                WHERE user_id = $1 AND group_id = $2 AND key = $3
                """,
                user_id,
                group_id,
                key,
            )
        if row is None:
            return None

        try:
            parsed = json.loads(str(row["value"]))
        except (TypeError, ValueError):
            logger.warning(
                "[KomariMemory] entity JSON parse failed: group={} user={} key={}",
                group_id,
                user_id,
                key,
            )
            return None
        return parsed if isinstance(parsed, dict) else None
