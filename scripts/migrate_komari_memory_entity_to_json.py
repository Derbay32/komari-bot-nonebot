"""将 komari_memory_entity 迁移到每用户两行 JSON 模型。

默认是 dry-run，仅打印迁移统计，不写库。

用法：
1. dry-run:
   poetry run python tools/migrate_komari_memory_entity_to_json.py
2. 执行迁移:
   poetry run python tools/migrate_komari_memory_entity_to_json.py --apply
3. 自定义配置路径:
   poetry run python tools/migrate_komari_memory_entity_to_json.py \
      --db-config-path config/config_manager/database_config.json \
      --bindings-path data/character_binding/bindings.json
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

logger = logging.getLogger("migrate_memory_entity")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

PROFILE_KEY = "user_profile"
PROFILE_CATEGORY = "profile_json"
INTERACTION_KEY = "interaction_history"
INTERACTION_CATEGORY = "interaction_history"
ALLOWED_CATEGORIES = {"preference", "fact", "relation", "general"}
CONSTRAINT_NAME = "ck_komari_memory_entity_two_row_model"


@dataclass(frozen=True)
class EntityRow:
    user_id: str
    group_id: str
    key: str
    value: str
    category: str
    importance: int
    last_accessed: datetime | None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _clamp_importance(value: Any, default: int = 3) -> int:
    try:
        val = int(value)
    except (TypeError, ValueError):
        val = default
    return max(1, min(5, val))


def _safe_json_dict(raw: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _load_bindings(path: Path) -> dict[str, str]:
    if not path.exists():
        logger.warning("character binding 文件不存在: %s", path)
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("character binding 文件解析失败: %s", path, exc_info=True)
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if str(v).strip()}


def _normalize_interaction(
    payload: dict[str, Any] | None,
    *,
    user_id: str,
    display_name: str,
) -> dict[str, Any]:
    interaction = dict(payload) if isinstance(payload, dict) else {}
    records = interaction.get("records")
    interaction["version"] = 1
    interaction["user_id"] = user_id
    interaction["display_name"] = display_name
    interaction["file_type"] = str(
        interaction.get("file_type", "用户的近期对鞠行为备忘录")
    )
    interaction["description"] = str(interaction.get("description", ""))
    interaction["records"] = records if isinstance(records, list) else []
    if len(interaction["records"]) > 6:
        interaction["records"] = interaction["records"][-6:]
    interaction["summary"] = str(interaction.get("summary", ""))
    interaction["updated_at"] = _now_iso()
    return interaction


def _normalize_profile(
    payload: dict[str, Any] | None,
    *,
    user_id: str,
    display_name: str,
    legacy_rows: list[EntityRow],
) -> dict[str, Any]:
    profile = dict(payload) if isinstance(payload, dict) else {}
    traits_raw = profile.get("traits")
    traits = dict(traits_raw) if isinstance(traits_raw, dict) else {}

    # 基于旧多行实体覆盖 traits（按 last_accessed 升序，后写覆盖前写）
    sorted_legacy = sorted(
        legacy_rows,
        key=lambda row: row.last_accessed or datetime.fromtimestamp(0, tz=UTC),
    )
    for row in sorted_legacy:
        key = row.key.strip()
        value = row.value.strip()
        if not key or not value:
            continue
        category = row.category if row.category in ALLOWED_CATEGORIES else "general"
        traits[key] = {
            "value": value,
            "category": category,
            "importance": _clamp_importance(row.importance),
            "updated_at": _now_iso(),
        }

    profile["version"] = 1
    profile["user_id"] = user_id
    profile["display_name"] = display_name
    profile["traits"] = traits
    profile["updated_at"] = _now_iso()
    return profile


def _build_target_rows(
    rows: list[EntityRow],
    *,
    bindings: dict[str, str],
) -> tuple[list[tuple[str, str, str, str, str, int]], int]:
    grouped: dict[tuple[str, str], list[EntityRow]] = {}
    for row in rows:
        grouped.setdefault((row.group_id, row.user_id), []).append(row)

    target_rows: list[tuple[str, str, str, str, str, int]] = []
    legacy_row_count = 0

    for (group_id, user_id), user_rows in grouped.items():
        profile_payload: dict[str, Any] | None = None
        interaction_payload: dict[str, Any] | None = None
        legacy_rows: list[EntityRow] = []

        for row in user_rows:
            if row.key == PROFILE_KEY:
                profile_payload = _safe_json_dict(row.value) or profile_payload
            elif row.key == INTERACTION_KEY:
                interaction_payload = _safe_json_dict(row.value) or interaction_payload
            else:
                legacy_rows.append(row)

        legacy_row_count += len(legacy_rows)
        profile_display_name = ""
        if isinstance(profile_payload, dict):
            profile_display_name = str(profile_payload.get("display_name", "")).strip()
        display_name = bindings.get(user_id) or profile_display_name

        normalized_profile = _normalize_profile(
            profile_payload,
            user_id=user_id,
            display_name=display_name,
            legacy_rows=legacy_rows,
        )
        normalized_interaction = _normalize_interaction(
            interaction_payload,
            user_id=user_id,
            display_name=display_name,
        )

        target_rows.append(
            (
                user_id,
                group_id,
                PROFILE_KEY,
                json.dumps(normalized_profile, ensure_ascii=False),
                PROFILE_CATEGORY,
                4,
            )
        )
        target_rows.append(
            (
                user_id,
                group_id,
                INTERACTION_KEY,
                json.dumps(normalized_interaction, ensure_ascii=False),
                INTERACTION_CATEGORY,
                5,
            )
        )

    return target_rows, legacy_row_count


async def _fetch_all_rows(pool: Any) -> list[EntityRow]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT user_id, group_id, key, value, category, importance, last_accessed
            FROM komari_memory_entity
            ORDER BY group_id, user_id, last_accessed NULLS LAST
            """
        )
    return [
        EntityRow(
            user_id=str(row["user_id"]),
            group_id=str(row["group_id"]),
            key=str(row["key"]),
            value=str(row["value"]),
            category=str(row["category"]),
            importance=int(row["importance"] or 3),
            last_accessed=row["last_accessed"],
        )
        for row in rows
    ]


async def _apply_constraints(conn: Any) -> None:
    await conn.execute(
        f"ALTER TABLE komari_memory_entity DROP CONSTRAINT IF EXISTS {CONSTRAINT_NAME}"
    )
    await conn.execute(
        f"""
        ALTER TABLE komari_memory_entity
        ADD CONSTRAINT {CONSTRAINT_NAME}
        CHECK (
            (key = '{PROFILE_KEY}' AND category = '{PROFILE_CATEGORY}')
            OR
            (key = '{INTERACTION_KEY}' AND category = '{INTERACTION_CATEGORY}')
        ) NOT VALID
        """
    )
    await conn.execute(
        f"ALTER TABLE komari_memory_entity VALIDATE CONSTRAINT {CONSTRAINT_NAME}"
    )


async def _verify_result(conn: Any) -> tuple[int, int]:
    invalid_keys = await conn.fetchval(
        """
        SELECT count(*)
        FROM komari_memory_entity
        WHERE key NOT IN ('user_profile', 'interaction_history')
           OR (
                key = 'user_profile' AND category != 'profile_json'
           )
           OR (
                key = 'interaction_history' AND category != 'interaction_history'
           )
        """
    )
    invalid_pairs = await conn.fetchval(
        """
        SELECT count(*) FROM (
            SELECT group_id, user_id, count(*) AS c
            FROM komari_memory_entity
            GROUP BY group_id, user_id
            HAVING count(*) != 2
        ) t
        """
    )
    return int(invalid_keys or 0), int(invalid_pairs or 0)


async def _apply_migration(
    pool: Any,
    *,
    target_rows: list[tuple[str, str, str, str, str, int]],
    create_backup: bool,
    apply_constraints: bool,
) -> None:
    backup_table_name = (
        f"komari_memory_entity_backup_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"
    )
    async with pool.acquire() as conn, conn.transaction():
        if create_backup:
            await conn.execute(
                f"CREATE TABLE {backup_table_name} AS TABLE komari_memory_entity"
            )
            logger.info("已创建备份表: %s", backup_table_name)

        await conn.execute("TRUNCATE TABLE komari_memory_entity")
        await conn.executemany(
            """
            INSERT INTO komari_memory_entity
            (user_id, group_id, key, value, category, importance)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            target_rows,
        )
        logger.info("已写入新结构行数: %s", len(target_rows))

        if apply_constraints:
            await _apply_constraints(conn)
            logger.info("已应用 key/category 约束: %s", CONSTRAINT_NAME)

        invalid_keys, invalid_pairs = await _verify_result(conn)
        if invalid_keys > 0 or invalid_pairs > 0:
            msg = (
                "迁移后校验失败: "
                f"invalid_keys={invalid_keys}, invalid_pairs={invalid_pairs}"
            )
            raise RuntimeError(msg)


async def main_async(
    *,
    db_config_path: Path,
    bindings_path: Path,
    apply: bool,
    create_backup: bool,
    apply_constraints: bool,
) -> None:
    db_config = load_database_config_from_file(db_config_path)
    pool = await create_postgres_pool(db_config, command_timeout=60)
    try:
        rows = await _fetch_all_rows(pool)
        bindings = _load_bindings(bindings_path)
        target_rows, legacy_row_count = _build_target_rows(rows, bindings=bindings)

        user_pairs = len({(row.group_id, row.user_id) for row in rows})
        logger.info("原始实体行: %s", len(rows))
        logger.info("用户对数量(group_id,user_id): %s", user_pairs)
        logger.info("旧格式行数(非 user_profile/interaction_history): %s", legacy_row_count)
        logger.info("目标行数(2 * 用户对): %s", len(target_rows))

        if not apply:
            logger.info("当前为 dry-run，未写入数据库。使用 --apply 执行迁移。")
            return

        await _apply_migration(
            pool,
            target_rows=target_rows,
            create_backup=create_backup,
            apply_constraints=apply_constraints,
        )
        logger.info("迁移完成。")
    finally:
        await pool.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="将 komari_memory_entity 迁移到每用户两行 JSON 模型"
    )
    parser.add_argument(
        "--db-config-path",
        type=Path,
        default=Path("config/config_manager/database_config.json"),
        help="数据库配置路径",
    )
    parser.add_argument(
        "--bindings-path",
        type=Path,
        default=Path("data/character_binding/bindings.json"),
        help="character binding 文件路径",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="执行写入（默认 dry-run）",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="迁移时不创建备份表（默认创建）",
    )
    parser.add_argument(
        "--no-constraints",
        action="store_true",
        help="迁移时不应用 key/category 约束",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(
        main_async(
            db_config_path=args.db_config_path,
            bindings_path=args.bindings_path,
            apply=args.apply,
            create_backup=not args.no_backup,
            apply_constraints=not args.no_constraints,
        )
    )


if __name__ == "__main__":
    main()
