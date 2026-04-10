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

    async def list_user_profiles(
        self,
        *,
        limit: int,
        offset: int,
        group_id: str | None = None,
        user_id: str | None = None,
        query: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """分页获取用户画像行。"""
        return await self._list_entity_rows(
            key=_PROFILE_KEY,
            limit=limit,
            offset=offset,
            group_id=group_id,
            user_id=user_id,
            query=query,
        )

    async def list_interaction_histories(
        self,
        *,
        limit: int,
        offset: int,
        group_id: str | None = None,
        user_id: str | None = None,
        query: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """分页获取互动历史行。"""
        return await self._list_entity_rows(
            key=_INTERACTION_KEY,
            limit=limit,
            offset=offset,
            group_id=group_id,
            user_id=user_id,
            query=query,
        )

    async def get_user_profile_row(
        self,
        *,
        user_id: str,
        group_id: str,
    ) -> dict[str, Any] | None:
        """读取带元数据的用户画像行。"""
        return await self._get_entity_row(
            user_id=user_id,
            group_id=group_id,
            key=_PROFILE_KEY,
        )

    async def get_interaction_history_row(
        self,
        *,
        user_id: str,
        group_id: str,
    ) -> dict[str, Any] | None:
        """读取带元数据的互动历史行。"""
        return await self._get_entity_row(
            user_id=user_id,
            group_id=group_id,
            key=_INTERACTION_KEY,
        )

    async def delete_user_profile(
        self,
        *,
        user_id: str,
        group_id: str,
    ) -> bool:
        """删除用户画像行。"""
        return await self._delete_entity_row(
            user_id=user_id,
            group_id=group_id,
            key=_PROFILE_KEY,
        )

    async def delete_interaction_history(
        self,
        *,
        user_id: str,
        group_id: str,
    ) -> bool:
        """删除互动历史行。"""
        return await self._delete_entity_row(
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
        entity_row = await self._get_entity_row(
            user_id=user_id,
            group_id=group_id,
            key=key,
        )
        if entity_row is None:
            return None
        value = entity_row.get("value")
        return value if isinstance(value, dict) else None

    async def _list_entity_rows(
        self,
        *,
        key: str,
        limit: int,
        offset: int,
        group_id: str | None = None,
        user_id: str | None = None,
        query: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        filters = [f"key = ${1}"]
        params: list[object] = [key]

        if group_id:
            filters.append(f"group_id = ${len(params) + 1}")
            params.append(group_id)
        if user_id:
            filters.append(f"user_id = ${len(params) + 1}")
            params.append(user_id)
        if query:
            filters.append(
                f"(user_id ILIKE ${len(params) + 1} OR value ILIKE ${len(params) + 1})"
            )
            params.append(f"%{query}%")

        where_sql = f"WHERE {' AND '.join(filters)}"

        async with self.pg_pool.acquire() as conn:
            total = await conn.fetchval(
                f"""
                SELECT COUNT(*)
                FROM komari_memory_entity
                {where_sql}
                """,
                *params,
            )
            rows = await conn.fetch(
                f"""
                SELECT
                    user_id,
                    group_id,
                    key,
                    category,
                    value,
                    importance,
                    access_count,
                    last_accessed
                FROM komari_memory_entity
                {where_sql}
                ORDER BY last_accessed DESC, user_id ASC
                LIMIT ${len(params) + 1}
                OFFSET ${len(params) + 2}
                """,
                *params,
                limit,
                offset,
            )

        parsed_rows: list[dict[str, Any]] = []
        for row in rows:
            parsed = self._parse_entity_row(dict(row))
            if parsed is not None:
                parsed_rows.append(parsed)
        return parsed_rows, int(total or 0)

    async def _get_entity_row(
        self,
        *,
        user_id: str,
        group_id: str,
        key: str,
    ) -> dict[str, Any] | None:
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    user_id,
                    group_id,
                    key,
                    category,
                    value,
                    importance,
                    access_count,
                    last_accessed
                FROM komari_memory_entity
                WHERE user_id = $1 AND group_id = $2 AND key = $3
                """,
                user_id,
                group_id,
                key,
            )
        return self._parse_entity_row(dict(row)) if row is not None else None

    async def _delete_entity_row(
        self,
        *,
        user_id: str,
        group_id: str,
        key: str,
    ) -> bool:
        async with self.pg_pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM komari_memory_entity
                WHERE user_id = $1 AND group_id = $2 AND key = $3
                """,
                user_id,
                group_id,
                key,
            )
        return result.endswith("1")

    def _parse_entity_row(self, row: dict[str, Any]) -> dict[str, Any] | None:
        try:
            parsed = json.loads(str(row["value"]))
        except (TypeError, ValueError):
            logger.warning(
                "[KomariMemory] entity JSON parse failed: group={} user={} key={}",
                row.get("group_id"),
                row.get("user_id"),
                row.get("key"),
            )
            return None

        if not isinstance(parsed, dict):
            return None

        row["value"] = parsed
        return row
