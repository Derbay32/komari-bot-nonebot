"""将 komari_memory_entity 拆分迁移到画像表与互动历史表。

默认 dry-run，仅统计可迁移行数，不写库。

用法：
1. dry-run:
   poetry run python scripts/split_komari_memory_entity_tables.py
2. 执行迁移:
   poetry run python scripts/split_komari_memory_entity_tables.py --apply
3. 仅处理指定群或用户:
   poetry run python scripts/split_komari_memory_entity_tables.py --apply --group-id 123456 --user-id 10001
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from komari_bot.common.database_config import load_database_config_from_file
from komari_bot.common.postgres import create_postgres_pool
from komari_bot.common.profile_compaction import normalize_profile_for_storage

logger = logging.getLogger("split_komari_memory_entity_tables")
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s | %(message)s",
    )

_LEGACY_TABLE = "komari_memory_entity"
_PROFILE_KEY = "user_profile"
_INTERACTION_KEY = "interaction_history"
_PROFILE_TABLE = "komari_memory_user_profile"
_INTERACTION_TABLE = "komari_memory_interaction_history"

_ENSURE_SPLIT_TABLES_SQL = (
    f"""
    CREATE TABLE IF NOT EXISTS {_PROFILE_TABLE} (
        user_id VARCHAR(64) NOT NULL,
        group_id VARCHAR(64) NOT NULL,
        version INT NOT NULL DEFAULT 1 CHECK (version >= 1),
        display_name TEXT NOT NULL,
        traits JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        importance INT DEFAULT 4 CHECK (importance BETWEEN 1 AND 5),
        access_count INT DEFAULT 0 CHECK (access_count >= 0),
        last_accessed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT ck_komari_memory_user_profile_traits_object
            CHECK (jsonb_typeof(traits) = 'object'),
        PRIMARY KEY (user_id, group_id)
    )
    """,
    f"""
    CREATE INDEX IF NOT EXISTS idx_komari_memory_user_profile_group
    ON {_PROFILE_TABLE}(group_id)
    """,
    f"""
    CREATE INDEX IF NOT EXISTS idx_komari_memory_user_profile_importance
    ON {_PROFILE_TABLE}(importance DESC)
    """,
    f"""
    CREATE INDEX IF NOT EXISTS idx_komari_memory_user_profile_display_name
    ON {_PROFILE_TABLE}(display_name)
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {_INTERACTION_TABLE} (
        user_id VARCHAR(64) NOT NULL,
        group_id VARCHAR(64) NOT NULL,
        version INT NOT NULL DEFAULT 1 CHECK (version >= 1),
        display_name TEXT NOT NULL,
        file_type TEXT NOT NULL DEFAULT '用户的近期对鞠行为备忘录',
        description TEXT NOT NULL DEFAULT '',
        summary TEXT NOT NULL DEFAULT '',
        records JSONB NOT NULL DEFAULT '[]'::jsonb,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        importance INT DEFAULT 5 CHECK (importance BETWEEN 1 AND 5),
        access_count INT DEFAULT 0 CHECK (access_count >= 0),
        last_accessed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT ck_komari_memory_interaction_history_records_array
            CHECK (jsonb_typeof(records) = 'array'),
        PRIMARY KEY (user_id, group_id)
    )
    """,
    f"""
    CREATE INDEX IF NOT EXISTS idx_komari_memory_interaction_history_group
    ON {_INTERACTION_TABLE}(group_id)
    """,
    f"""
    CREATE INDEX IF NOT EXISTS idx_komari_memory_interaction_history_importance
    ON {_INTERACTION_TABLE}(importance DESC)
    """,
    f"""
    CREATE INDEX IF NOT EXISTS idx_komari_memory_interaction_history_display_name
    ON {_INTERACTION_TABLE}(display_name)
    """,
)


@dataclass(frozen=True)
class LegacyProfileRow:
    user_id: str
    group_id: str
    value: str | None
    profile_version: int | None
    profile_display_name: str | None
    profile_traits: Any
    profile_updated_at: Any
    importance: int
    access_count: int
    last_accessed: Any


@dataclass(frozen=True)
class LegacyInteractionRow:
    user_id: str
    group_id: str
    value: str | None
    importance: int
    access_count: int
    last_accessed: Any


async def _table_exists(pool: Any, table_name: str) -> bool:
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = current_schema()
                  AND table_name = $1
            )
            """,
            table_name,
        )
    return bool(exists)


async def _fetch_entity_columns(pool: Any) -> set[str]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = $1
            """,
            _LEGACY_TABLE,
        )
    return {str(row["column_name"]) for row in rows}


def _optional_column_sql(column_name: str, available_columns: set[str]) -> str:
    if column_name in available_columns:
        return column_name
    return f"NULL AS {column_name}"


async def _fetch_legacy_profile_rows(
    pool: Any,
    *,
    group_id: str | None,
    user_id: str | None,
) -> list[LegacyProfileRow]:
    if not await _table_exists(pool, _LEGACY_TABLE):
        return []

    available_columns = await _fetch_entity_columns(pool)
    conditions = ["key = $1"]
    args: list[Any] = [_PROFILE_KEY]
    if group_id:
        conditions.append(f"group_id = ${len(args) + 1}")
        args.append(group_id)
    if user_id:
        conditions.append(f"user_id = ${len(args) + 1}")
        args.append(user_id)

    where_clause = " AND ".join(conditions)
    query = f"""
        SELECT
            user_id,
            group_id,
            value,
            {_optional_column_sql('profile_version', available_columns)},
            {_optional_column_sql('profile_display_name', available_columns)},
            {_optional_column_sql('profile_traits', available_columns)},
            {_optional_column_sql('profile_updated_at', available_columns)},
            importance,
            access_count,
            last_accessed
        FROM {_LEGACY_TABLE}
        WHERE {where_clause}
        ORDER BY group_id, user_id
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *args)

    return [
        LegacyProfileRow(
            user_id=str(row["user_id"]),
            group_id=str(row["group_id"]),
            value=str(row["value"]) if row["value"] is not None else None,
            profile_version=(
                int(row["profile_version"]) if row["profile_version"] is not None else None
            ),
            profile_display_name=(
                str(row["profile_display_name"])
                if row["profile_display_name"] is not None
                else None
            ),
            profile_traits=row["profile_traits"],
            profile_updated_at=row["profile_updated_at"],
            importance=int(row["importance"] or 4),
            access_count=int(row["access_count"] or 0),
            last_accessed=row["last_accessed"],
        )
        for row in rows
    ]


async def _fetch_legacy_interaction_rows(
    pool: Any,
    *,
    group_id: str | None,
    user_id: str | None,
) -> list[LegacyInteractionRow]:
    if not await _table_exists(pool, _LEGACY_TABLE):
        return []

    conditions = ["key = $1"]
    args: list[Any] = [_INTERACTION_KEY]
    if group_id:
        conditions.append(f"group_id = ${len(args) + 1}")
        args.append(group_id)
    if user_id:
        conditions.append(f"user_id = ${len(args) + 1}")
        args.append(user_id)

    where_clause = " AND ".join(conditions)
    query = f"""
        SELECT
            user_id,
            group_id,
            value,
            importance,
            access_count,
            last_accessed
        FROM {_LEGACY_TABLE}
        WHERE {where_clause}
        ORDER BY group_id, user_id
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *args)

    return [
        LegacyInteractionRow(
            user_id=str(row["user_id"]),
            group_id=str(row["group_id"]),
            value=str(row["value"]) if row["value"] is not None else None,
            importance=int(row["importance"] or 5),
            access_count=int(row["access_count"] or 0),
            last_accessed=row["last_accessed"],
        )
        for row in rows
    ]


def _build_profile_payload(row: LegacyProfileRow) -> dict[str, Any] | None:
    if (
        row.profile_display_name is not None
        and row.profile_traits is not None
        and row.profile_updated_at is not None
    ):
        traits_raw = row.profile_traits
        if isinstance(traits_raw, str):
            try:
                traits_raw = json.loads(traits_raw)
            except (TypeError, ValueError):
                traits_raw = None
        updated_at = row.profile_updated_at
        if hasattr(updated_at, "isoformat"):
            updated_at = updated_at.isoformat()
        return {
            "version": max(1, int(row.profile_version or 1)),
            "user_id": row.user_id,
            "display_name": str(row.profile_display_name).strip() or row.user_id,
            "traits": dict(traits_raw) if isinstance(traits_raw, dict) else {},
            "updated_at": str(updated_at or "").strip(),
        }

    if row.value is None:
        return None

    try:
        parsed = json.loads(row.value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _build_interaction_payload(
    row: LegacyInteractionRow,
    *,
    fallback_display_name: str,
) -> dict[str, Any] | None:
    if row.value is None:
        return None
    try:
        parsed = json.loads(row.value)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None

    records = parsed.get("records")
    if isinstance(records, str):
        try:
            records = json.loads(records)
        except (TypeError, ValueError):
            records = None

    updated_at = parsed.get("updated_at")
    if hasattr(updated_at, "isoformat"):
        updated_at = updated_at.isoformat()

    return {
        "version": max(1, _coerce_int(parsed.get("version"), default=1)),
        "user_id": row.user_id,
        "display_name": (
            str(parsed.get("display_name", "")).strip()
            or fallback_display_name
            or row.user_id
        ),
        "file_type": (
            str(parsed.get("file_type", "")).strip() or "用户的近期对鞠行为备忘录"
        ),
        "description": str(parsed.get("description", "")).strip(),
        "summary": str(parsed.get("summary", "")).strip(),
        "records": list(records) if isinstance(records, list) else [],
        "updated_at": str(updated_at or "").strip(),
    }


def _coerce_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_timestamptz(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    text = str(value or "").strip()
    if text:
        normalized_text = f"{text[:-1]}+00:00" if text.endswith("Z") else text
        try:
            parsed = datetime.fromisoformat(normalized_text)
        except ValueError:
            logger.warning(
                "[KomariMemory] 时间字段解析失败，回退当前时间: raw=%s",
                text,
            )
        else:
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return datetime.now(UTC)


async def _ensure_split_tables(pool: Any) -> None:
    async with pool.acquire() as conn:
        for statement in _ENSURE_SPLIT_TABLES_SQL:
            await conn.execute(statement)


async def _migrate_profiles(
    conn: Any,
    *,
    rows: list[LegacyProfileRow],
) -> tuple[dict[tuple[str, str], str], int]:
    display_name_map: dict[tuple[str, str], str] = {}
    migrated = 0
    for row in rows:
        payload = _build_profile_payload(row)
        if payload is None:
            logger.warning(
                "[KomariMemory] 跳过无法解析的旧画像行: group=%s user=%s",
                row.group_id,
                row.user_id,
            )
            continue

        normalized = normalize_profile_for_storage(
            payload,
            fallback_user_id=row.user_id,
            fallback_display_name=str(payload.get("display_name", "")).strip(),
        )
        display_name = str(normalized.get("display_name", "")).strip() or row.user_id
        display_name_map[(row.group_id, row.user_id)] = display_name

        await conn.execute(
            f"""
            INSERT INTO {_PROFILE_TABLE} (
                user_id,
                group_id,
                version,
                display_name,
                traits,
                updated_at,
                importance,
                access_count,
                last_accessed
            )
            VALUES ($1, $2, $3, $4, $5::jsonb, $6::timestamptz, $7, $8, $9)
            ON CONFLICT (user_id, group_id)
            DO UPDATE SET
                version = EXCLUDED.version,
                display_name = EXCLUDED.display_name,
                traits = EXCLUDED.traits,
                updated_at = EXCLUDED.updated_at,
                importance = EXCLUDED.importance,
                access_count = GREATEST({_PROFILE_TABLE}.access_count, EXCLUDED.access_count),
                last_accessed = COALESCE(EXCLUDED.last_accessed, {_PROFILE_TABLE}.last_accessed)
            """,
            row.user_id,
            row.group_id,
            max(1, _coerce_int(normalized.get("version"), default=1)),
            display_name,
            json.dumps(normalized.get("traits", {}), ensure_ascii=False),
            _normalize_timestamptz(normalized.get("updated_at")),
            row.importance,
            row.access_count,
            row.last_accessed,
        )
        migrated += 1

    return display_name_map, migrated


async def _migrate_interactions(
    conn: Any,
    *,
    rows: list[LegacyInteractionRow],
    display_name_map: dict[tuple[str, str], str],
) -> int:
    migrated = 0
    for row in rows:
        payload = _build_interaction_payload(
            row,
            fallback_display_name=display_name_map.get((row.group_id, row.user_id), ""),
        )
        if payload is None:
            logger.warning(
                "[KomariMemory] 跳过无法解析的旧互动历史行: group=%s user=%s",
                row.group_id,
                row.user_id,
            )
            continue

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
                importance,
                access_count,
                last_accessed
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::timestamptz, $10, $11, $12)
            ON CONFLICT (user_id, group_id)
            DO UPDATE SET
                version = EXCLUDED.version,
                display_name = EXCLUDED.display_name,
                file_type = EXCLUDED.file_type,
                description = EXCLUDED.description,
                summary = EXCLUDED.summary,
                records = EXCLUDED.records,
                updated_at = EXCLUDED.updated_at,
                importance = EXCLUDED.importance,
                access_count = GREATEST({_INTERACTION_TABLE}.access_count, EXCLUDED.access_count),
                last_accessed = COALESCE(EXCLUDED.last_accessed, {_INTERACTION_TABLE}.last_accessed)
            """,
            row.user_id,
            row.group_id,
            max(1, _coerce_int(payload.get("version"), default=1)),
            str(payload.get("display_name", "")).strip() or row.user_id,
            str(payload.get("file_type", "")).strip() or "用户的近期对鞠行为备忘录",
            str(payload.get("description", "")).strip(),
            str(payload.get("summary", "")).strip(),
            json.dumps(payload.get("records", []), ensure_ascii=False),
            _normalize_timestamptz(payload.get("updated_at")),
            row.importance,
            row.access_count,
            row.last_accessed,
        )
        migrated += 1

    return migrated


async def run(
    *,
    apply: bool,
    database_config_path: Path,
    group_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, int]:
    database_config = load_database_config_from_file(database_config_path)
    pool = await create_postgres_pool(database_config, command_timeout=60)

    try:
        profile_rows = await _fetch_legacy_profile_rows(
            pool,
            group_id=group_id,
            user_id=user_id,
        )
        interaction_rows = await _fetch_legacy_interaction_rows(
            pool,
            group_id=group_id,
            user_id=user_id,
        )

        stats = {
            "profile_rows": len(profile_rows),
            "interaction_rows": len(interaction_rows),
            "migrated_profiles": 0,
            "migrated_interactions": 0,
        }

        logger.info(
            "[KomariMemory] 拆表迁移启动: profiles=%s interactions=%s apply=%s group=%s user=%s",
            len(profile_rows),
            len(interaction_rows),
            apply,
            group_id or "-",
            user_id or "-",
        )

        if not apply:
            return stats

        await _ensure_split_tables(pool)
        async with pool.acquire() as conn, conn.transaction():
            display_name_map, migrated_profiles = await _migrate_profiles(
                conn,
                rows=profile_rows,
            )
            migrated_interactions = await _migrate_interactions(
                conn,
                rows=interaction_rows,
                display_name_map=display_name_map,
            )
            stats["migrated_profiles"] = migrated_profiles
            stats["migrated_interactions"] = migrated_interactions

        logger.info(
            "[KomariMemory] 拆表迁移完成: profiles=%s interactions=%s",
            stats["migrated_profiles"],
            stats["migrated_interactions"],
        )
        return stats
    finally:
        await pool.close()


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="将 komari_memory_entity 拆分到双表")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="执行真实迁移；默认仅 dry-run",
    )
    parser.add_argument(
        "--database-config-path",
        type=Path,
        default=Path("config/config_manager/database_config.json"),
        help="共享数据库配置文件路径",
    )
    parser.add_argument("--group-id", type=str, default=None, help="仅处理指定群号")
    parser.add_argument("--user-id", type=str, default=None, help="仅处理指定用户")
    return parser


def main() -> None:
    args = _build_argument_parser().parse_args()
    asyncio.run(
        run(
            apply=bool(args.apply),
            database_config_path=args.database_config_path,
            group_id=args.group_id,
            user_id=args.user_id,
        )
    )


if __name__ == "__main__":
    main()
