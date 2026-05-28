"""提取 komari_memory_entity.user_profile 中的画像列。

默认 dry-run，仅统计可迁移行数与异常行数。

用法：
1. dry-run:
   poetry run python scripts/extract_komari_memory_profile_columns.py
2. 执行迁移:
   poetry run python scripts/extract_komari_memory_profile_columns.py --apply
3. 执行迁移并切换最终严格约束:
   poetry run python scripts/extract_komari_memory_profile_columns.py --apply --apply-constraints
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

_PROFILE_KEY = "user_profile"
_PROFILE_CATEGORY = "profile_json"
logger = logging.getLogger("extract_komari_memory_profile_columns")
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s | %(message)s",
    )

_ENSURE_PROFILE_COLUMNS_SQL = (
    """
    ALTER TABLE komari_memory_entity
    ADD COLUMN IF NOT EXISTS profile_version INT
    """,
    """
    ALTER TABLE komari_memory_entity
    ADD COLUMN IF NOT EXISTS profile_payload_user_id VARCHAR(64)
    """,
    """
    ALTER TABLE komari_memory_entity
    ADD COLUMN IF NOT EXISTS profile_display_name TEXT
    """,
    """
    ALTER TABLE komari_memory_entity
    ADD COLUMN IF NOT EXISTS profile_traits JSONB
    """,
    """
    ALTER TABLE komari_memory_entity
    ADD COLUMN IF NOT EXISTS profile_updated_at TIMESTAMPTZ
    """,
    """
    ALTER TABLE komari_memory_entity
    ALTER COLUMN value DROP NOT NULL
    """,
)

_FINAL_CONSTRAINT_SQL = (
    """
    ALTER TABLE komari_memory_entity
    DROP CONSTRAINT IF EXISTS ck_komari_memory_entity_two_row_model
    """,
    """
    ALTER TABLE komari_memory_entity
    ADD CONSTRAINT ck_komari_memory_entity_two_row_model
    CHECK (
        (
            key = 'user_profile'
            AND category = 'profile_json'
            AND value IS NULL
            AND profile_version IS NOT NULL
            AND profile_payload_user_id IS NOT NULL
            AND profile_display_name IS NOT NULL
            AND profile_traits IS NOT NULL
            AND profile_updated_at IS NOT NULL
        )
        OR
        (
            key = 'interaction_history'
            AND category = 'interaction_history'
            AND value IS NOT NULL
            AND profile_version IS NULL
            AND profile_payload_user_id IS NULL
            AND profile_display_name IS NULL
            AND profile_traits IS NULL
            AND profile_updated_at IS NULL
        )
    ) NOT VALID
    """,
    """
    ALTER TABLE komari_memory_entity
    VALIDATE CONSTRAINT ck_komari_memory_entity_two_row_model
    """,
)


@dataclass(frozen=True)
class ProfileRow:
    user_id: str
    group_id: str
    value: str | None
    profile_version: int | None
    profile_payload_user_id: str | None
    profile_display_name: str | None
    profile_traits: Any
    profile_updated_at: Any


async def _ensure_profile_columns(pool: Any) -> None:
    async with pool.acquire() as conn:
        for statement in _ENSURE_PROFILE_COLUMNS_SQL:
            await conn.execute(statement)


async def _apply_final_constraint(pool: Any) -> None:
    async with pool.acquire() as conn:
        for statement in _FINAL_CONSTRAINT_SQL:
            await conn.execute(statement)


async def _fetch_profile_rows(
    pool: Any,
    *,
    group_id: str | None = None,
    user_id: str | None = None,
) -> list[ProfileRow]:
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
            {_optional_column_sql('profile_payload_user_id', available_columns)},
            {_optional_column_sql('profile_display_name', available_columns)},
            {_optional_column_sql('profile_traits', available_columns)},
            {_optional_column_sql('profile_updated_at', available_columns)}
        FROM komari_memory_entity
        WHERE {where_clause}
        ORDER BY group_id, user_id
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *args)

    return [
        ProfileRow(
            user_id=str(row["user_id"]),
            group_id=str(row["group_id"]),
            value=str(row["value"]) if row["value"] is not None else None,
            profile_version=(
                int(row["profile_version"]) if row["profile_version"] is not None else None
            ),
            profile_payload_user_id=(
                str(row["profile_payload_user_id"])
                if row["profile_payload_user_id"] is not None
                else None
            ),
            profile_display_name=(
                str(row["profile_display_name"])
                if row["profile_display_name"] is not None
                else None
            ),
            profile_traits=row["profile_traits"],
            profile_updated_at=row["profile_updated_at"],
        )
        for row in rows
    ]


async def _fetch_entity_columns(pool: Any) -> set[str]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'komari_memory_entity'
            """
        )
    return {str(row["column_name"]) for row in rows}


def _optional_column_sql(column_name: str, available_columns: set[str]) -> str:
    if column_name in available_columns:
        return column_name
    return f"NULL AS {column_name}"


def _build_profile_payload(row: ProfileRow) -> dict[str, Any] | None:
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
            "user_id": str(row.profile_payload_user_id or row.user_id).strip()
            or row.user_id,
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


def _normalize_profile_updated_at(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    text = str(value or "").strip()
    if text:
        normalized_text = f"{text[:-1]}+00:00" if text.endswith("Z") else text
        try:
            parsed = datetime.fromisoformat(normalized_text)
        except ValueError:
            logger.warning(
                "[KomariMemory] 画像 updated_at 解析失败，回退当前时间: raw=%s",
                text,
            )
        else:
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)

    return datetime.now(UTC)


async def _migrate_profile_row(
    pool: Any,
    *,
    row: ProfileRow,
    normalized_profile: dict[str, Any],
    payload_user_id: str,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE komari_memory_entity
            SET value = NULL,
                category = $4,
                profile_version = $5,
                profile_payload_user_id = $6,
                profile_display_name = $7,
                profile_traits = $8::jsonb,
                profile_updated_at = $9::timestamptz
            WHERE user_id = $1 AND group_id = $2 AND key = $3
            """,
            row.user_id,
            row.group_id,
            _PROFILE_KEY,
            _PROFILE_CATEGORY,
            int(normalized_profile.get("version", 1) or 1),
            payload_user_id,
            str(normalized_profile.get("display_name", "")).strip() or row.user_id,
            json.dumps(normalized_profile.get("traits", {}), ensure_ascii=False),
            _normalize_profile_updated_at(normalized_profile.get("updated_at")),
        )


async def _count_legacy_rows(pool: Any) -> int:
    async with pool.acquire() as conn:
        value = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM komari_memory_entity
            WHERE key = 'user_profile'
              AND (
                    value IS NOT NULL
                    OR profile_version IS NULL
                    OR profile_payload_user_id IS NULL
                    OR profile_display_name IS NULL
                    OR profile_traits IS NULL
                    OR profile_updated_at IS NULL
              )
            """
        )
    return int(value or 0)


async def run(
    *,
    apply: bool,
    apply_constraints: bool,
    database_config_path: Path,
    group_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, int]:
    database_config = load_database_config_from_file(database_config_path)
    pool = await create_postgres_pool(database_config, command_timeout=60)

    try:
        if apply:
            await _ensure_profile_columns(pool)

        rows = await _fetch_profile_rows(pool, group_id=group_id, user_id=user_id)
        stats = {
            "scanned": len(rows),
            "legacy_rows": 0,
            "already_columnized": 0,
            "migrated": 0,
            "payload_user_id_mismatch": 0,
            "failed": 0,
        }

        logger.info(
            "[KomariMemory] 画像列提取启动: rows=%s apply=%s constraints=%s group=%s user=%s",
            len(rows),
            apply,
            apply_constraints,
            group_id or "-",
            user_id or "-",
        )

        for row in rows:
            payload = _build_profile_payload(row)
            if payload is None:
                logger.warning(
                    "[KomariMemory] 跳过无法解析的画像行: group=%s user=%s",
                    row.group_id,
                    row.user_id,
                )
                stats["failed"] += 1
                continue

            payload_user_id = str(payload.get("user_id", "")).strip() or row.user_id
            if payload_user_id != row.user_id:
                stats["payload_user_id_mismatch"] += 1

            normalized_profile = normalize_profile_for_storage(
                payload,
                fallback_user_id=row.user_id,
                fallback_display_name=str(payload.get("display_name", "")).strip(),
            )

            is_legacy_row = (
                row.value is not None
                or row.profile_version is None
                or row.profile_payload_user_id is None
                or row.profile_display_name is None
                or row.profile_traits is None
                or row.profile_updated_at is None
            )
            if is_legacy_row:
                stats["legacy_rows"] += 1
            else:
                stats["already_columnized"] += 1

            if not apply:
                continue

            await _migrate_profile_row(
                pool,
                row=row,
                normalized_profile=normalized_profile,
                payload_user_id=payload_user_id,
            )
            stats["migrated"] += 1

        if apply and apply_constraints:
            legacy_rows = await _count_legacy_rows(pool)
            if legacy_rows > 0:
                msg = f"仍有 {legacy_rows} 条画像行未完成列迁移，无法切换最终约束"
                raise RuntimeError(msg)
            await _apply_final_constraint(pool)

        logger.info(
            "[KomariMemory] 画像列提取完成: scanned=%s legacy=%s columnized=%s migrated=%s mismatched_payload_uid=%s failed=%s",
            stats["scanned"],
            stats["legacy_rows"],
            stats["already_columnized"],
            stats["migrated"],
            stats["payload_user_id_mismatch"],
            stats["failed"],
        )
        return stats
    finally:
        await pool.close()


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="提取 komari_memory 用户画像列")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="执行真实迁移；默认仅 dry-run",
    )
    parser.add_argument(
        "--apply-constraints",
        action="store_true",
        help="迁移完成后切换为最终严格约束",
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
            apply_constraints=bool(args.apply_constraints),
            database_config_path=args.database_config_path,
            group_id=args.group_id,
            user_id=args.user_id,
        )
    )


if __name__ == "__main__":
    main()
