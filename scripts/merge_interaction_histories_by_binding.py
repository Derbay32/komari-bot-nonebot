"""按绑定表将互动历史合并到规范 uid。

默认 dry-run，仅输出受影响的分组信息，不写库。

用法：
1. dry-run:
   poetry run python scripts/merge_interaction_histories_by_binding.py
2. 执行真实合并:
   poetry run python scripts/merge_interaction_histories_by_binding.py --apply
3. 仅处理指定群或用户:
   poetry run python scripts/merge_interaction_histories_by_binding.py --apply --group-id 123456 --user-id 10001
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

logger = logging.getLogger("merge_interaction_histories_by_binding")
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s | %(message)s",
    )

_TABLE = "komari_memory_interaction_history"
_DEFAULT_FILE_TYPE = "用户的近期对鞠行为备忘录"


@dataclass(frozen=True)
class InteractionRow:
    user_id: str
    group_id: str
    version: int
    display_name: str
    file_type: str
    description: str
    summary: str
    records: list[dict[str, Any]]
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


def _normalize_records(raw: Any) -> list[dict[str, Any]]:
    parsed = raw
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except (TypeError, ValueError):
            parsed = None
    if not isinstance(parsed, list):
        return []

    normalized: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                "event": str(item.get("event", "")).strip(),
                "result": str(item.get("result", "")).strip(),
                "emotion": str(item.get("emotion", "")).strip(),
            }
        )
    return normalized


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


def _format_dt(value: datetime | None) -> str:
    return value.isoformat() if isinstance(value, datetime) else ""


def _coerce_timestamp(value: Any) -> datetime | None:
    parsed = _coerce_dt(value)
    if parsed is None:
        return None
    return parsed.astimezone(UTC).replace(tzinfo=None)


def _dedupe_records(rows: list[InteractionRow]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: item.updated_at or datetime.fromtimestamp(0, tz=UTC)):
        for record in row.records:
            signature = json.dumps(record, ensure_ascii=False, sort_keys=True)
            if signature in seen:
                continue
            seen.add(signature)
            merged.append(record)
    return merged


def _pick_latest_text(
    rows: list[InteractionRow],
    *,
    field: str,
    default: str = "",
) -> str:
    chosen = default
    chosen_time = datetime.fromtimestamp(0, tz=UTC)
    for row in rows:
        value = str(getattr(row, field, "") or "").strip()
        if not value:
            continue
        updated_at = row.updated_at or datetime.fromtimestamp(0, tz=UTC)
        if updated_at >= chosen_time:
            chosen_time = updated_at
            chosen = value
    return chosen


def _build_merge_plan(
    rows: list[InteractionRow],
    *,
    target_uid: str,
    display_name: str,
) -> MergePlan:
    source_user_ids = tuple(sorted({row.user_id for row in rows}))
    records = _dedupe_records(rows)
    merged_updated_at = max(
        (row.updated_at for row in rows if row.updated_at is not None),
        default=datetime.now(UTC),
    )
    merged_last_accessed = max(
        (row.last_accessed for row in rows if row.last_accessed is not None),
        default=None,
    )
    merged_payload = {
        "version": max((row.version for row in rows), default=1),
        "user_id": target_uid,
        "display_name": display_name,
        "file_type": _pick_latest_text(
            rows,
            field="file_type",
            default=_DEFAULT_FILE_TYPE,
        )
        or _DEFAULT_FILE_TYPE,
        "description": _pick_latest_text(rows, field="description", default=""),
        "summary": _pick_latest_text(rows, field="summary", default=""),
        "records": records,
        "updated_at": _format_dt(merged_updated_at),
    }
    delete_user_ids = tuple(uid for uid in source_user_ids if uid != target_uid)

    return MergePlan(
        group_id=rows[0].group_id,
        display_name=display_name,
        target_uid=target_uid,
        source_user_ids=source_user_ids,
        delete_user_ids=delete_user_ids,
        merged_payload=merged_payload,
        merged_importance=max((row.importance for row in rows), default=5),
        merged_access_count=sum(row.access_count for row in rows),
        merged_last_accessed=merged_last_accessed,
    )


def _row_requires_merge(
    rows: list[InteractionRow],
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
) -> list[InteractionRow]:
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
                file_type,
                description,
                summary,
                records,
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
        InteractionRow(
            user_id=str(row["user_id"]),
            group_id=str(row["group_id"]),
            version=max(1, int(row["version"] or 1)),
            display_name=str(row["display_name"] or "").strip(),
            file_type=str(row["file_type"] or "").strip(),
            description=str(row["description"] or "").strip(),
            summary=str(row["summary"] or "").strip(),
            records=_normalize_records(row["records"]),
            updated_at=_coerce_dt(row["updated_at"]),
            importance=int(row["importance"] or 5),
            access_count=int(row["access_count"] or 0),
            last_accessed=_coerce_timestamp(row["last_accessed"]),
        )
        for row in rows
    ]


def _build_merge_plans(
    rows: list[InteractionRow],
    *,
    bindings: dict[str, str],
) -> list[MergePlan]:
    rows_by_group_and_name: dict[tuple[str, str], list[InteractionRow]] = defaultdict(list)
    rows_by_group_and_uid: dict[tuple[str, str], InteractionRow] = {}
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
            access_count = EXCLUDED.access_count,
            last_accessed = EXCLUDED.last_accessed
        """,
        plan.target_uid,
        plan.group_id,
        int(payload.get("version", 1) or 1),
        plan.display_name,
        str(payload.get("file_type", "")).strip() or _DEFAULT_FILE_TYPE,
        str(payload.get("description", "")).strip(),
        str(payload.get("summary", "")).strip(),
        json.dumps(payload.get("records", []), ensure_ascii=False),
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
            "[KomariMemory] 互动历史绑定合并启动: rows=%s plans=%s affected_rows=%s apply=%s group=%s user=%s",
            len(rows),
            len(plans),
            affected_rows,
            apply,
            group_id or "-",
            user_id or "-",
        )
        for plan in plans:
            logger.info(
                "[KomariMemory] merge plan: group=%s display_name=%s target_uid=%s source_uids=%s delete_uids=%s summary=%s records=%s",
                plan.group_id,
                plan.display_name,
                plan.target_uid,
                ",".join(plan.source_user_ids),
                ",".join(plan.delete_user_ids) or "-",
                str(plan.merged_payload.get("summary", "")),
                len(plan.merged_payload.get("records", [])),
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
    parser = argparse.ArgumentParser(description="按绑定表合并互动历史")
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
