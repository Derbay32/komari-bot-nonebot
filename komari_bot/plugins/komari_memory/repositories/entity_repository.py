"""实体数据访问仓库（画像表 + 互动历史表）。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nonebot import logger

if TYPE_CHECKING:
    import asyncpg

_PROFILE_KEY = "user_profile"
_PROFILE_CATEGORY = "profile_json"
_PROFILE_TABLE = "komari_memory_user_profile"

_INTERACTION_KEY = "interaction_history"
_INTERACTION_CATEGORY = "interaction_history"
_INTERACTION_TABLE = "komari_memory_interaction_history"


class EntityRepository:
    """画像与互动历史数据访问仓库。"""

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
        """写入用户画像。"""
        display_name = str(profile.get("display_name", "")).strip() or user_id
        traits = self._normalize_json_object(profile.get("traits"))
        updated_at = self._normalize_timestamptz(profile.get("updated_at"))
        version = self._coerce_version(profile.get("version"))

        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {_PROFILE_TABLE} (
                    user_id,
                    group_id,
                    version,
                    display_name,
                    traits,
                    updated_at,
                    importance
                )
                VALUES ($1, $2, $3, $4, $5::jsonb, $6::timestamptz, $7)
                ON CONFLICT (user_id, group_id)
                DO UPDATE SET
                    version = EXCLUDED.version,
                    display_name = EXCLUDED.display_name,
                    traits = EXCLUDED.traits,
                    updated_at = EXCLUDED.updated_at,
                    importance = EXCLUDED.importance
                """,
                user_id,
                group_id,
                version,
                display_name,
                json.dumps(traits, ensure_ascii=False),
                updated_at,
                importance,
            )
        logger.debug(
            "[KomariMemory] upsert profile row: group={} user={}",
            group_id,
            user_id,
        )

    async def upsert_interaction_history(
        self,
        *,
        user_id: str,
        group_id: str,
        interaction: dict[str, Any],
        importance: int = 5,
    ) -> None:
        """写入互动历史。"""
        display_name = str(interaction.get("display_name", "")).strip() or user_id
        file_type = (
            str(interaction.get("file_type", "")).strip()
            or "用户的近期对鞠行为备忘录"
        )
        description = str(interaction.get("description", "")).strip()
        summary = str(interaction.get("summary", "")).strip()
        records = self._normalize_json_array(interaction.get("records"))
        updated_at = self._normalize_timestamptz(interaction.get("updated_at"))
        version = self._coerce_version(interaction.get("version"))

        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {_INTERACTION_TABLE} (
                    user_id,
                    group_id,
                    version,
                    display_name,
                    file_type,
                    description,
                    summary,
                    records,
                    updated_at,
                    importance
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::timestamptz, $10)
                ON CONFLICT (user_id, group_id)
                DO UPDATE SET
                    version = EXCLUDED.version,
                    display_name = EXCLUDED.display_name,
                    file_type = EXCLUDED.file_type,
                    description = EXCLUDED.description,
                    summary = EXCLUDED.summary,
                    records = EXCLUDED.records,
                    updated_at = EXCLUDED.updated_at,
                    importance = EXCLUDED.importance
                """,
                user_id,
                group_id,
                version,
                display_name,
                file_type,
                description,
                summary,
                json.dumps(records, ensure_ascii=False),
                updated_at,
                importance,
            )
        logger.debug(
            "[KomariMemory] upsert interaction row: group={} user={}",
            group_id,
            user_id,
        )

    async def get_user_profile(
        self,
        *,
        user_id: str,
        group_id: str,
    ) -> dict[str, Any] | None:
        """读取用户画像。"""
        entity_row = await self.get_user_profile_row(user_id=user_id, group_id=group_id)
        if entity_row is None:
            return None
        value = entity_row.get("value")
        return value if isinstance(value, dict) else None

    async def get_interaction_history(
        self,
        *,
        user_id: str,
        group_id: str,
    ) -> dict[str, Any] | None:
        """读取互动历史。"""
        entity_row = await self.get_interaction_history_row(
            user_id=user_id,
            group_id=group_id,
        )
        if entity_row is None:
            return None
        value = entity_row.get("value")
        return value if isinstance(value, dict) else None

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
        filters: list[str] = []
        params: list[object] = []
        self._append_common_filters(
            filters=filters,
            params=params,
            group_id=group_id,
            user_id=user_id,
        )
        if query:
            params.append(f"%{query}%")
            placeholder = len(params)
            filters.append(
                f"(user_id ILIKE ${placeholder} "
                f"OR display_name ILIKE ${placeholder} "
                f"OR traits::text ILIKE ${placeholder})"
            )

        where_sql = self._build_where_sql(filters)
        async with self.pg_pool.acquire() as conn:
            total = await conn.fetchval(
                f"""
                SELECT COUNT(*)
                FROM {_PROFILE_TABLE}
                {where_sql}
                """,
                *params,
            )
            rows = await conn.fetch(
                f"""
                SELECT
                    user_id,
                    group_id,
                    version,
                    display_name,
                    traits,
                    updated_at,
                    importance,
                    access_count,
                    last_accessed
                FROM {_PROFILE_TABLE}
                {where_sql}
                ORDER BY last_accessed DESC, user_id ASC
                LIMIT ${len(params) + 1}
                OFFSET ${len(params) + 2}
                """,
                *params,
                limit,
                offset,
            )

        parsed_rows = [self._parse_profile_row(dict(row)) for row in rows]
        return parsed_rows, int(total or 0)

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
        filters: list[str] = []
        params: list[object] = []
        self._append_common_filters(
            filters=filters,
            params=params,
            group_id=group_id,
            user_id=user_id,
        )
        if query:
            params.append(f"%{query}%")
            placeholder = len(params)
            filters.append(
                f"(user_id ILIKE ${placeholder} "
                f"OR display_name ILIKE ${placeholder} "
                f"OR file_type ILIKE ${placeholder} "
                f"OR description ILIKE ${placeholder} "
                f"OR summary ILIKE ${placeholder} "
                f"OR records::text ILIKE ${placeholder})"
            )

        where_sql = self._build_where_sql(filters)
        async with self.pg_pool.acquire() as conn:
            total = await conn.fetchval(
                f"""
                SELECT COUNT(*)
                FROM {_INTERACTION_TABLE}
                {where_sql}
                """,
                *params,
            )
            rows = await conn.fetch(
                f"""
                SELECT
                    user_id,
                    group_id,
                    version,
                    display_name,
                    file_type,
                    description,
                    summary,
                    records,
                    updated_at,
                    importance,
                    access_count,
                    last_accessed
                FROM {_INTERACTION_TABLE}
                {where_sql}
                ORDER BY last_accessed DESC, user_id ASC
                LIMIT ${len(params) + 1}
                OFFSET ${len(params) + 2}
                """,
                *params,
                limit,
                offset,
            )

        parsed_rows = [self._parse_interaction_row(dict(row)) for row in rows]
        return parsed_rows, int(total or 0)

    async def get_user_profile_row(
        self,
        *,
        user_id: str,
        group_id: str,
    ) -> dict[str, Any] | None:
        """读取带元数据的用户画像行。"""
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT
                    user_id,
                    group_id,
                    version,
                    display_name,
                    traits,
                    updated_at,
                    importance,
                    access_count,
                    last_accessed
                FROM {_PROFILE_TABLE}
                WHERE user_id = $1 AND group_id = $2
                """,
                user_id,
                group_id,
            )
        return self._parse_profile_row(dict(row)) if row is not None else None

    async def get_interaction_history_row(
        self,
        *,
        user_id: str,
        group_id: str,
    ) -> dict[str, Any] | None:
        """读取带元数据的互动历史行。"""
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT
                    user_id,
                    group_id,
                    version,
                    display_name,
                    file_type,
                    description,
                    summary,
                    records,
                    updated_at,
                    importance,
                    access_count,
                    last_accessed
                FROM {_INTERACTION_TABLE}
                WHERE user_id = $1 AND group_id = $2
                """,
                user_id,
                group_id,
            )
        return self._parse_interaction_row(dict(row)) if row is not None else None

    async def delete_user_profile(
        self,
        *,
        user_id: str,
        group_id: str,
    ) -> bool:
        """删除用户画像行。"""
        async with self.pg_pool.acquire() as conn:
            result = await conn.execute(
                f"""
                DELETE FROM {_PROFILE_TABLE}
                WHERE user_id = $1 AND group_id = $2
                """,
                user_id,
                group_id,
            )
        return result.endswith("1")

    async def delete_interaction_history(
        self,
        *,
        user_id: str,
        group_id: str,
    ) -> bool:
        """删除互动历史行。"""
        async with self.pg_pool.acquire() as conn:
            result = await conn.execute(
                f"""
                DELETE FROM {_INTERACTION_TABLE}
                WHERE user_id = $1 AND group_id = $2
                """,
                user_id,
                group_id,
            )
        return result.endswith("1")

    def _append_common_filters(
        self,
        *,
        filters: list[str],
        params: list[object],
        group_id: str | None,
        user_id: str | None,
    ) -> None:
        if group_id:
            params.append(group_id)
            filters.append(f"group_id = ${len(params)}")
        if user_id:
            params.append(user_id)
            filters.append(f"user_id = ${len(params)}")

    def _build_where_sql(self, filters: list[str]) -> str:
        if not filters:
            return ""
        return f"WHERE {' AND '.join(filters)}"

    def _parse_profile_row(self, row: dict[str, Any]) -> dict[str, Any]:
        user_id = str(row.get("user_id", "")).strip()
        display_name = str(row.get("display_name", "")).strip() or user_id
        value = {
            "version": self._coerce_version(row.get("version")),
            "user_id": user_id,
            "display_name": display_name,
            "traits": self._normalize_json_object(row.get("traits")),
            "updated_at": self._format_datetime(row.get("updated_at")),
        }
        return {
            "user_id": user_id,
            "group_id": str(row.get("group_id", "")).strip(),
            "key": _PROFILE_KEY,
            "category": _PROFILE_CATEGORY,
            "importance": int(row.get("importance", 4) or 4),
            "access_count": int(row.get("access_count", 0) or 0),
            "last_accessed": row.get("last_accessed"),
            "value": value,
        }

    def _parse_interaction_row(self, row: dict[str, Any]) -> dict[str, Any]:
        user_id = str(row.get("user_id", "")).strip()
        display_name = str(row.get("display_name", "")).strip() or user_id
        value = {
            "version": self._coerce_version(row.get("version")),
            "user_id": user_id,
            "display_name": display_name,
            "file_type": (
                str(row.get("file_type", "")).strip() or "用户的近期对鞠行为备忘录"
            ),
            "description": str(row.get("description", "")).strip(),
            "summary": str(row.get("summary", "")).strip(),
            "records": self._normalize_json_array(row.get("records")),
            "updated_at": self._format_datetime(row.get("updated_at")),
        }
        return {
            "user_id": user_id,
            "group_id": str(row.get("group_id", "")).strip(),
            "key": _INTERACTION_KEY,
            "category": _INTERACTION_CATEGORY,
            "importance": int(row.get("importance", 5) or 5),
            "access_count": int(row.get("access_count", 0) or 0),
            "last_accessed": row.get("last_accessed"),
            "value": value,
        }

    def _normalize_json_object(self, value: Any) -> dict[str, Any]:
        parsed = value
        if isinstance(parsed, str):
            try:
                parsed = json.loads(parsed)
            except (TypeError, ValueError):
                parsed = None
        return dict(parsed) if isinstance(parsed, dict) else {}

    def _normalize_json_array(self, value: Any) -> list[Any]:
        parsed = value
        if isinstance(parsed, str):
            try:
                parsed = json.loads(parsed)
            except (TypeError, ValueError):
                parsed = None
        return list(parsed) if isinstance(parsed, list) else []

    def _coerce_version(self, value: Any) -> int:
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return 1

    def _format_datetime(self, value: Any) -> str:
        if isinstance(value, datetime):
            return value.isoformat()
        text = str(value or "").strip()
        return text

    def _normalize_timestamptz(self, value: Any) -> datetime:
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

        text = str(value or "").strip()
        if text:
            normalized_text = f"{text[:-1]}+00:00" if text.endswith("Z") else text
            try:
                parsed = datetime.fromisoformat(normalized_text)
            except ValueError:
                logger.warning(
                    "[KomariMemory] 时间字段解析失败，回退当前时间: raw={}",
                    text,
                )
            else:
                return parsed if parsed.tzinfo is not None else parsed.replace(
                    tzinfo=UTC
                )
        return datetime.now(UTC)
