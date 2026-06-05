"""将 komari_decision YAML scenes 全量迁移到 PostgreSQL 内容表。"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from komari_bot.common.database_config import get_shared_database_config
from komari_bot.common.postgres import create_postgres_pool
from komari_bot.plugins.komari_decision.repositories.scene_repository import (
    SceneRepository,
)
from komari_bot.plugins.komari_decision.services.scene_template_loader import (
    YamlSceneTemplateLoader,
)

_REQUIRED_FIXED_KEYS = {"NOISE", "MEANINGFUL", "CALL_DIRECT", "CALL_MENTION"}
_DEFAULT_SOURCE_PATH = Path("config") / "prompts" / "komari_memory_scenes.yaml"


@dataclass(frozen=True)
class MigrationStats:
    """迁移差异统计。"""

    added: int
    updated: int
    deleted: int
    unchanged: int
    fixed_count: int
    general_count: int
    source_hash: str


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-path",
        type=Path,
        default=_DEFAULT_SOURCE_PATH,
        help="旧 YAML scenes 文件路径，默认 config/prompts/komari_memory_scenes.yaml",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只展示差异，不写入 PostgreSQL",
    )
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="允许覆盖非空 komari_decision_scenes 表并删除不在 YAML 中的旧项",
    )
    return parser.parse_args()


def _validate_items(items: list[Any]) -> None:
    keys: set[str] = set()
    fixed_keys: set[str] = set()
    general_count = 0
    for item in items:
        scene_key = item.scene_key.strip()
        scene_type = item.scene_type.strip()
        content_text = item.content_text.strip()
        if not scene_key:
            msg = "scene_key 不能为空"
            raise ValueError(msg)
        if scene_key in keys:
            msg = f"scene_key 重复: {scene_key}"
            raise ValueError(msg)
        keys.add(scene_key)
        if scene_type not in {"fixed", "general"}:
            msg = f"scene_type 非法: {scene_key}={scene_type}"
            raise ValueError(msg)
        if not content_text:
            msg = f"content_text 不能为空: {scene_key}"
            raise ValueError(msg)
        if scene_type == "fixed":
            fixed_keys.add(scene_key)
        else:
            general_count += 1

    missing = sorted(_REQUIRED_FIXED_KEYS - fixed_keys)
    if missing:
        msg = f"缺少必需 fixed keys: {missing}"
        raise ValueError(msg)
    if general_count <= 0:
        msg = "general_scenes 不能为空"
        raise ValueError(msg)


def _build_stats(target_items: list[Any], existing_rows: list[dict[str, Any]], source_hash: str) -> MigrationStats:
    existing = {str(row["scene_key"]): row for row in existing_rows}
    target = {item.scene_key: item for item in target_items}
    added = 0
    updated = 0
    unchanged = 0
    for key, item in target.items():
        row = existing.get(key)
        if row is None:
            added += 1
            continue
        changed = (
            str(row["scene_type"]) != item.scene_type
            or str(row["content_text"]) != item.content_text
            or str(row["content_hash"]) != item.content_hash
            or bool(row["enabled"]) != item.enabled
            or int(row["order_index"]) != item.order_index
        )
        if changed:
            updated += 1
        else:
            unchanged += 1

    deleted = len(set(existing) - set(target))
    fixed_count = sum(1 for item in target_items if item.scene_type == "fixed")
    general_count = sum(1 for item in target_items if item.scene_type == "general")
    return MigrationStats(
        added=added,
        updated=updated,
        deleted=deleted,
        unchanged=unchanged,
        fixed_count=fixed_count,
        general_count=general_count,
        source_hash=source_hash,
    )


def _print_stats(stats: MigrationStats, *, dry_run: bool) -> None:
    mode = "dry-run" if dry_run else "已写入"
    print(  # noqa: T201
        f"[{mode}] 新增={stats.added} 更新={stats.updated} 删除旧项={stats.deleted} "
        f"未变化={stats.unchanged} fixed={stats.fixed_count} general={stats.general_count} "
        f"source_hash={stats.source_hash}"
    )


async def _run() -> None:
    args = _parse_args()
    loader = YamlSceneTemplateLoader(args.source_path)
    payload = loader.load_scene_template()
    _validate_items(payload.items)

    pool = await create_postgres_pool(get_shared_database_config())
    try:
        repository = SceneRepository(pool)
        await repository.ensure_schema()
        existing_rows = await repository.list_scenes(enabled_only=False)
        stats = _build_stats(payload.items, existing_rows, payload.source_hash)
        if args.dry_run:
            _print_stats(stats, dry_run=True)
            return
        if existing_rows and not args.replace_existing:
            msg = "komari_decision_scenes 非空；如需全量覆盖请添加 --replace-existing"
            raise RuntimeError(msg)

        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("LOCK TABLE komari_decision_scenes IN EXCLUSIVE MODE")
            target_keys = [item.scene_key for item in payload.items]
            for item in payload.items:
                await conn.execute(
                    """
                    INSERT INTO komari_decision_scenes
                    (scene_key, scene_type, content_text, content_hash, enabled, order_index)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (scene_key) DO UPDATE
                    SET scene_type = EXCLUDED.scene_type,
                        content_text = EXCLUDED.content_text,
                        content_hash = EXCLUDED.content_hash,
                        enabled = EXCLUDED.enabled,
                        order_index = EXCLUDED.order_index,
                        updated_at = NOW()
                    """,
                    item.scene_key,
                    item.scene_type,
                    item.content_text,
                    item.content_hash,
                    item.enabled,
                    item.order_index,
                )
            await conn.execute(
                """
                DELETE FROM komari_decision_scenes
                WHERE NOT (scene_key = ANY($1::text[]))
                """,
                target_keys,
            )
        _print_stats(stats, dry_run=False)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(_run())
