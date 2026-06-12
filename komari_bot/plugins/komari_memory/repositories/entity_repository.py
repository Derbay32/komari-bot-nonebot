"""实体数据访问仓库（画像表 + 互动历史表）。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NotRequired, TypedDict

from nonebot import logger

from komari_bot.common.sql_like_utils import escape_like_pattern

if TYPE_CHECKING:
    from collections.abc import Sequence

    import asyncpg

_PROFILE_KEY = "user_profile"
_PROFILE_CATEGORY = "profile_json"
_PROFILE_TABLE = "komari_memory_user_profile"

_INTERACTION_KEY = "interaction_history"
_INTERACTION_CATEGORY = "interaction_history"
_INTERACTION_TABLE = "komari_memory_interaction_history"


class UserProfileUpsertPayload(TypedDict):
    """批量写入用户画像的轻量载荷。"""

    user_id: str
    group_id: str
    profile: dict[str, Any]
    importance: int


class UserProfileTraitsPatchPayload(TypedDict):
    """批量增量写入用户画像 traits 的载荷。"""

    user_id: str
    group_id: str
    display_name: str
    set_traits: dict[str, dict[str, Any]]
    delete_keys: list[str]
    importance: int
    updated_at: NotRequired[datetime | str | None]
    snapshot_updated_at: NotRequired[datetime | str | None]


class UserProfileConcurrentUpdateError(RuntimeError):
    """画像写入时检测到 snapshot 条件冲突。"""

    def __init__(self, user_id: str, group_id: str) -> None:
        super().__init__(f"用户画像已被并发更新: group={group_id} user={user_id}")
        self.user_id = user_id
        self.group_id = group_id


@dataclass(frozen=True)
class UserProfileRow:
    """用户画像写入返回行。"""

    user_id: str
    group_id: str
    version: int
    traits: dict[str, Any]
    updated_at: datetime


@dataclass(frozen=True)
class UserProfileConflict:
    """用户画像乐观锁冲突。"""

    user_id: str
    group_id: str
    snapshot_updated_at: datetime | str | None = None


@dataclass(frozen=True)
class UserProfileUpsertError:
    """用户画像单条写入错误。"""

    user_id: str
    group_id: str
    message: str


@dataclass(frozen=True)
class UserProfileBatchUpsertResult:
    """用户画像批量写入结果。"""

    upserted: list[UserProfileRow] = field(default_factory=list)
    conflicts: list[UserProfileConflict] = field(default_factory=list)
    errors: list[UserProfileUpsertError] = field(default_factory=list)


class UserProfileBatchUpsertError(RuntimeError):
    """用户画像批量写入存在单条数据库错误。"""

    def __init__(self, result: UserProfileBatchUpsertResult) -> None:
        super().__init__("用户画像批量写入存在部分失败")
        self.result = result
        self.upserted = result.upserted
        self.errors = result.errors


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
        result = await self.batch_upsert_user_profiles(
            [
                {
                    "user_id": user_id,
                    "group_id": group_id,
                    "profile": profile,
                    "importance": importance,
                }
            ]
        )
        if result.conflicts:
            conflict = result.conflicts[0]
            raise UserProfileConcurrentUpdateError(
                user_id=conflict.user_id,
                group_id=conflict.group_id,
            )
        logger.debug(
            "[KomariMemory] upsert profile row: group={} user={}",
            group_id,
            user_id,
        )

    async def batch_upsert_user_profiles(
        self,
        profiles: Sequence[UserProfileTraitsPatchPayload | UserProfileUpsertPayload],
    ) -> UserProfileBatchUpsertResult:
        """逐条隔离事务批量增量写入用户画像。"""
        result = UserProfileBatchUpsertResult()
        if not profiles:
            return result

        async with self.pg_pool.acquire() as conn:
            for raw_payload in profiles:
                payload = self._normalize_profile_patch_payload(raw_payload)
                try:
                    async with conn.transaction():
                        row = await conn.fetchrow(
                            self._profile_upsert_sql(),
                            payload["user_id"],
                            payload["group_id"],
                            payload["display_name"],
                            json.dumps(payload["set_traits"], ensure_ascii=False),
                            payload["delete_keys"],
                            self._normalize_timestamptz(payload.get("updated_at")),
                            payload["importance"],
                            self._normalize_optional_timestamptz(
                                payload.get("snapshot_updated_at")
                            ),
                        )
                except Exception as exc:
                    logger.exception(
                        "[KomariMemory] profile row upsert failed: group={} user={}",
                        payload["group_id"],
                        payload["user_id"],
                    )
                    result.errors.append(
                        UserProfileUpsertError(
                            user_id=payload["user_id"],
                            group_id=payload["group_id"],
                            message=str(exc),
                        )
                    )
                    continue

                if row is None:
                    result.conflicts.append(
                        UserProfileConflict(
                            user_id=payload["user_id"],
                            group_id=payload["group_id"],
                            snapshot_updated_at=payload.get("snapshot_updated_at"),
                        )
                    )
                    continue

                result.upserted.append(self._parse_profile_upsert_row(dict(row)))

        if result.errors:
            raise UserProfileBatchUpsertError(result)

        logger.debug(
            "[KomariMemory] batch upsert profile rows: upserted={}, conflicts={}",
            len(result.upserted),
            len(result.conflicts),
        )
        return result

    def _parse_profile_upsert_row(self, row: dict[str, Any]) -> UserProfileRow:
        """解析 profile upsert RETURNING 行。"""
        traits = row.get("traits")
        updated_at = row.get("updated_at")
        return UserProfileRow(
            user_id=str(row["user_id"]),
            group_id=str(row["group_id"]),
            version=int(row["version"]),
            traits=dict(traits) if isinstance(traits, dict) else {},
            updated_at=updated_at if isinstance(updated_at, datetime) else datetime.now(UTC),
        )

    async def upsert_interaction_history(
        self,
        *,
        user_id: str,
        group_id: str,
        interaction: dict[str, Any],
        importance: int = 5,
    ) -> None:
        """旧 PG JSONB 互动历史写入入口已停用。"""
        del user_id, group_id, interaction, importance
        msg = "旧 interaction_history records JSONB 写入入口已停用"
        raise RuntimeError(msg)

    def _profile_upsert_sql(self) -> str:
        """返回用户画像 upsert SQL。"""
        return f"""
            INSERT INTO {_PROFILE_TABLE} (
                user_id,
                group_id,
                version,
                display_name,
                traits,
                updated_at,
                importance
            )
            VALUES ($1, $2, 1, $3, ($4::jsonb - $5::text[]), $6::timestamptz, $7)
            ON CONFLICT (user_id, group_id)
            DO UPDATE SET
                version = {_PROFILE_TABLE}.version + 1,
                display_name = EXCLUDED.display_name,
                traits = (({_PROFILE_TABLE}.traits || $4::jsonb) - $5::text[]),
                updated_at = EXCLUDED.updated_at,
                importance = EXCLUDED.importance
            WHERE $8::timestamptz IS NULL
               OR {_PROFILE_TABLE}.updated_at <= $8::timestamptz
            RETURNING user_id, group_id, version, traits, updated_at
            """

    def _normalize_profile_patch_payload(
        self,
        payload: UserProfileTraitsPatchPayload | UserProfileUpsertPayload,
    ) -> UserProfileTraitsPatchPayload:
        user_id = payload["user_id"]
        group_id = payload["group_id"]
        importance = payload["importance"]
        if "profile" not in payload:
            return {
                "user_id": user_id,
                "group_id": group_id,
                "display_name": str(payload.get("display_name", "")).strip() or user_id,
                "set_traits": self._normalize_traits_patch(payload.get("set_traits")),
                "delete_keys": self._normalize_delete_keys(payload.get("delete_keys")),
                "updated_at": payload.get("updated_at"),
                "snapshot_updated_at": payload.get("snapshot_updated_at"),
                "importance": importance,
            }

        profile = payload["profile"]
        return {
            "user_id": user_id,
            "group_id": group_id,
            "display_name": str(profile.get("display_name", "")).strip() or user_id,
            "set_traits": self._normalize_traits_patch(profile.get("traits")),
            "delete_keys": [],
            "updated_at": profile.get("updated_at"),
            "snapshot_updated_at": None,
            "importance": importance,
        }

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
            params.append(f"%{escape_like_pattern(query)}%")
            placeholder = len(params)
            filters.append(
                f"(user_id ILIKE ${placeholder} ESCAPE '\\' "
                f"OR display_name ILIKE ${placeholder} ESCAPE '\\' "
                f"OR traits::text ILIKE ${placeholder} ESCAPE '\\')"
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
            params.append(f"%{escape_like_pattern(query)}%")
            placeholder = len(params)
            filters.append(
                f"(user_id ILIKE ${placeholder} ESCAPE '\\' "
                f"OR display_name ILIKE ${placeholder} ESCAPE '\\' "
                f"OR file_type ILIKE ${placeholder} ESCAPE '\\' "
                f"OR description ILIKE ${placeholder} ESCAPE '\\' "
                f"OR summary ILIKE ${placeholder} ESCAPE '\\' "
                f"OR records::text ILIKE ${placeholder} ESCAPE '\\')"
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

    def _normalize_traits_patch(self, value: Any) -> dict[str, dict[str, Any]]:
        traits = self._normalize_json_object(value)
        normalized: dict[str, dict[str, Any]] = {}
        for raw_key, raw_payload in traits.items():
            key = str(raw_key).strip()
            if key and isinstance(raw_payload, dict):
                normalized[key] = dict(raw_payload)
        return normalized

    def _normalize_delete_keys(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        seen: set[str] = set()
        keys: list[str] = []
        for raw_key in value:
            key = str(raw_key).strip()
            if key and key not in seen:
                keys.append(key)
                seen.add(key)
        return keys

    def _coerce_version(self, value: Any) -> int:
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return 1

    def _format_datetime(self, value: Any) -> str:
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value or "").strip()

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
                return (
                    parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
                )
        return datetime.now(UTC)

    def _normalize_optional_timestamptz(self, value: Any) -> datetime | None:
        if value is None or str(value).strip() == "":
            return None
        return self._normalize_timestamptz(value)
