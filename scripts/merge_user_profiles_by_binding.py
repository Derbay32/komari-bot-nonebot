"""按绑定表将用户画像合并到规范 uid。

默认 dry-run，仅输出受影响的分组信息，不写库。

用法：
1. dry-run:
   poetry run python scripts/merge_user_profiles_by_binding.py
2. 执行真实合并:
   poetry run python scripts/merge_user_profiles_by_binding.py --apply
3. 仅处理指定群或用户:
   poetry run python scripts/merge_user_profiles_by_binding.py --apply --group-id 123456 --user-id 10001
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections import defaultdict
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

logger = logging.getLogger("merge_user_profiles_by_binding")
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s | %(message)s",
    )

_TABLE = "komari_memory_user_profile"


@dataclass(frozen=True)
class ProfileRow:
    user_id: str
    group_id: str
    version: int
    display_name: str
    traits: dict[str, dict[str, Any]]
    updated_at: datetime | None
    importance: int
    access_count: int
    last_accessed: datetime | None


@dataclass(frozen=True)
class MergePlan:
    group_id: str
    display_name: str
    target_uid: str
    source_user_ids: tuple[str, ...]
    delete_user_ids: tuple[str, ...]
    merged_payload: dict[str, Any]
    merged_importance: int
    merged_access_count: int
    merged_last_accessed: datetime | None


def _load_bindings(path: Path) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {}
    return {
        str(value).strip(): str(key).strip()
        for key, value in payload.items()
        if str(value).strip()
    }


def _coerce_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    text = str(value).strip()
    if not text:
        return None
    normalized_text = f"{text[:-1]}+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized_text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _coerce_timestamp(value: Any) -> datetime | None:
    parsed = _coerce_dt(value)
    if parsed is None:
        return None
    return parsed.astimezone(UTC).replace(tzinfo=None)


def _normalize_traits(raw: Any) -> dict[str, dict[str, Any]]:
    parsed = raw
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except (TypeError, ValueError):
            parsed = None
    if not isinstance(parsed, dict):
        return {}

    traits: dict[str, dict[str, Any]] = {}
    for key, value in parsed.items():
        if not isinstance(value, dict):
            continue
        normalized_key = str(key).strip()
        normalized_value = str(value.get("value", "")).strip()
        if not normalized_key or not normalized_value:
            continue
        try:
            importance = int(value.get("importance", 3))
        except (TypeError, ValueError):
            importance = 3
        traits[normalized_key] = {
            "value": normalized_value,
            "category": str(value.get("category", "general")).strip() or "general",
            "importance": max(1, min(5, importance)),
            "updated_at": str(value.get("updated_at", "")).strip(),
        }
    return traits


def _trait_rank(payload: dict[str, Any]) -> tuple[int, datetime]:
    updated_at = _coerce_dt(payload.get("updated_at")) or datetime.fromtimestamp(0, tz=UTC)
    try:
        importance = int(payload.get("importance", 3))
    except (TypeError, ValueError):
        importance = 3
    return (max(1, min(5, importance)), updated_at)


def _merge_traits(rows: list[ProfileRow]) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        for key, payload in row.traits.items():
            existing = merged.get(key)
            if existing is None or _trait_rank(payload) >= _trait_rank(existing):
                merged[key] = dict(payload)
    return merged


def _row_requires_merge(
    rows: list[ProfileRow],
    *,
    target_uid: str,
) -> bool:
    if len(rows) > 1:
        return True
    return bool(rows) and rows[0].user_id != target_uid


async def _fetch_rows(
    pool: Any,
    *,
    group_id: str | None,
    user_id: str | None,
) -> list[ProfileRow]:
    filters: list[str] = []
    args: list[Any] = []
    if group_id:
        filters.append(f"group_id = ${len(args) + 1}")
        args.append(group_id)
    if user_id:
        filters.append(f"user_id = ${len(args) + 1}")
        args.append(user_id)
    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""

    async with pool.acquire() as conn:
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
            FROM {_TABLE}
            {where_sql}
            ORDER BY group_id, display_name, user_id
            """,
            *args,
        )

    return [
        ProfileRow(
            user_id=str(row["user_id"]),
            group_id=str(row["group_id"]),
            version=max(1, int(row["version"] or 1)),
            display_name=str(row["display_name"] or "").strip(),
            traits=_normalize_traits(row["traits"]),
            updated_at=_coerce_dt(row["updated_at"]),
            importance=int(row["importance"] or 4),
            access_count=int(row["access_count"] or 0),
            last_accessed=_coerce_timestamp(row["last_accessed"]),
        )
        for row in rows
    ]


def _build_merge_plan(
    rows: list[ProfileRow],
    *,
    target_uid: str,
    display_name: str,
) -> MergePlan:
    source_user_ids = tuple(sorted({row.user_id for row in rows}))
    merged_payload = normalize_profile_for_storage(
        {
            "version": max((row.version for row in rows), default=1),
            "user_id": target_uid,
            "display_name": display_name,
            "traits": _merge_traits(rows),
            "updated_at": (
                max(
                    (row.updated_at for row in rows if row.updated_at is not None),
                    default=datetime.now(UTC),
                ).isoformat()
            ),
        },
        fallback_user_id=target_uid,
        fallback_display_name=display_name,
    )
    delete_user_ids = tuple(uid for uid in source_user_ids if uid != target_uid)
    merged_last_accessed = max(
        (row.last_accessed for row in rows if row.last_accessed is not None),
        default=None,
    )
    return MergePlan(
        group_id=rows[0].group_id,
        display_name=display_name,
        target_uid=target_uid,
        source_user_ids=source_user_ids,
        delete_user_ids=delete_user_ids,
        merged_payload=merged_payload,
        merged_importance=max((row.importance for row in rows), default=4),
        merged_access_count=sum(row.access_count for row in rows),
        merged_last_accessed=merged_last_accessed,
    )


def _build_merge_plans(
    rows: list[ProfileRow],
    *,
    bindings: dict[str, str],
) -> list[MergePlan]:
    rows_by_group_and_name: dict[tuple[str, str], list[ProfileRow]] = defaultdict(list)
    rows_by_group_and_uid: dict[tuple[str, str], ProfileRow] = {}
    for row in rows:
        rows_by_group_and_uid[(row.group_id, row.user_id)] = row
        if row.display_name:
            rows_by_group_and_name[(row.group_id, row.display_name)].append(row)

    plans: list[MergePlan] = []
    planned_targets: set[tuple[str, str]] = set()
    for (group_id, display_name), grouped_rows in sorted(rows_by_group_and_name.items()):
        target_uid = bindings.get(display_name)
        if not target_uid:
            continue
        if not _row_requires_merge(grouped_rows, target_uid=target_uid):
            continue
        target_key = (group_id, target_uid)
        merge_rows = list(grouped_rows)
        existing_target = rows_by_group_and_uid.get(target_key)
        if existing_target is not None and existing_target not in merge_rows:
            merge_rows.append(existing_target)
        if target_key in planned_targets:
            continue
        planned_targets.add(target_key)
        plans.append(
            _build_merge_plan(
                merge_rows,
                target_uid=target_uid,
                display_name=display_name,
            )
        )
    return plans


async def _apply_plan(conn: Any, plan: MergePlan) -> None:
    payload = plan.merged_payload
    await conn.execute(
        f"""
        INSERT INTO {_TABLE} (
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
            access_count = EXCLUDED.access_count,
            last_accessed = EXCLUDED.last_accessed
        """,
        plan.target_uid,
        plan.group_id,
        int(payload.get("version", 1) or 1),
        plan.display_name,
        json.dumps(payload.get("traits", {}), ensure_ascii=False),
        _coerce_dt(payload.get("updated_at")) or datetime.now(UTC),
        plan.merged_importance,
        plan.merged_access_count,
        _coerce_timestamp(plan.merged_last_accessed),
    )

    if not plan.delete_user_ids:
        return

    await conn.execute(
        f"""
        DELETE FROM {_TABLE}
        WHERE group_id = $1
          AND user_id = ANY($2::varchar[])
        """,
        plan.group_id,
        list(plan.delete_user_ids),
    )


async def run(
    *,
    apply: bool,
    database_config_path: Path,
    bindings_path: Path,
    group_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, int]:
    bindings = _load_bindings(bindings_path)
    database_config = load_database_config_from_file(database_config_path)
    pool = await create_postgres_pool(database_config, command_timeout=60)

    try:
        rows = await _fetch_rows(pool, group_id=group_id, user_id=user_id)
        plans = _build_merge_plans(rows, bindings=bindings)
        affected_rows = sum(len(plan.source_user_ids) for plan in plans)

        logger.info(
            "[KomariMemory] 用户画像绑定合并启动: rows=%s plans=%s affected_rows=%s apply=%s group=%s user=%s",
            len(rows),
            len(plans),
            affected_rows,
            apply,
            group_id or "-",
            user_id or "-",
        )
        for plan in plans:
            logger.info(
                "[KomariMemory] merge plan: group=%s display_name=%s target_uid=%s source_uids=%s delete_uids=%s traits=%s",
                plan.group_id,
                plan.display_name,
                plan.target_uid,
                ",".join(plan.source_user_ids),
                ",".join(plan.delete_user_ids) or "-",
                len(plan.merged_payload.get("traits", {})),
            )

        if apply and plans:
            async with pool.acquire() as conn, conn.transaction():
                for plan in plans:
                    await _apply_plan(conn, plan)

        return {
            "rows": len(rows),
            "plans": len(plans),
            "affected_rows": affected_rows,
        }
    finally:
        await pool.close()


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按绑定表合并用户画像")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="执行真实合并；默认仅 dry-run",
    )
    parser.add_argument(
        "--database-config-path",
        type=Path,
        default=Path("config/config_manager/database_config.json"),
        help="共享数据库配置文件路径",
    )
    parser.add_argument(
        "--bindings-path",
        type=Path,
        default=Path("data/character_binding/bindings.json"),
        help="character binding 文件路径",
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
            bindings_path=args.bindings_path,
            group_id=args.group_id,
            user_id=args.user_id,
        )
    )


if __name__ == "__main__":
    main()
