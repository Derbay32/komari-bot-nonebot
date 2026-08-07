"""将 komari_decision YAML scenes 全量迁移到 PostgreSQL 内容表。

本脚本是独立迁移工具，不导入 ``komari_bot`` 项目代码；数据库连接信息从
dotenv / 环境变量读取。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg
import yaml

_REQUIRED_FIXED_KEYS = {"NOISE", "MEANINGFUL", "CALL_DIRECT", "CALL_MENTION"}
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_SOURCE_PATH = _PROJECT_ROOT / "config" / "prompts" / "komari_memory_scenes.yaml"
_DEFAULT_ENV_PATH = _PROJECT_ROOT / ".env"

_SCENE_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS komari_memory_scene_set (
        id BIGSERIAL PRIMARY KEY,
        source_path TEXT NOT NULL,
        source_hash TEXT NOT NULL,
        embedding_model TEXT NOT NULL,
        embedding_instruction_hash TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('BUILDING', 'READY', 'FAILED')),
        item_total INT NOT NULL DEFAULT 0 CHECK (item_total >= 0),
        item_ready INT NOT NULL DEFAULT 0 CHECK (item_ready >= 0),
        item_failed INT NOT NULL DEFAULT 0 CHECK (item_failed >= 0),
        error_message TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        ready_at TIMESTAMPTZ
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_komari_memory_scene_set_status
    ON komari_memory_scene_set(status, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_komari_memory_scene_set_source_hash
    ON komari_memory_scene_set(source_hash)
    """,
    """
    CREATE TABLE IF NOT EXISTS komari_decision_scenes (
        id BIGSERIAL PRIMARY KEY,
        scene_key TEXT NOT NULL UNIQUE,
        scene_type TEXT NOT NULL CHECK (scene_type IN ('fixed', 'general')),
        content_text TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        order_index INT NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_komari_decision_scenes_type_order
    ON komari_decision_scenes(scene_type, enabled, order_index)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_komari_decision_scenes_content_hash
    ON komari_decision_scenes(content_hash)
    """,
    """
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = 'komari_memory_scene_item'
              AND column_name = 'scene_key'
        ) THEN
            DROP TABLE komari_memory_scene_item CASCADE;
        END IF;
    END $$
    """,
    """
    CREATE TABLE IF NOT EXISTS komari_memory_scene_item (
        id BIGSERIAL PRIMARY KEY,
        set_id BIGINT NOT NULL REFERENCES komari_memory_scene_set(id) ON DELETE CASCADE,
        scene_id BIGINT NOT NULL REFERENCES komari_decision_scenes(id) ON DELETE CASCADE,
        content_hash TEXT NOT NULL,
        embedding REAL[],
        embedding_dim INT,
        status TEXT NOT NULL CHECK (status IN ('PENDING', 'READY', 'FAILED')),
        error_message TEXT,
        embedded_at TIMESTAMPTZ,
        UNIQUE (set_id, scene_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_komari_memory_scene_item_scene_id
    ON komari_memory_scene_item(scene_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_komari_memory_scene_item_set_status
    ON komari_memory_scene_item(set_id, status)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_komari_memory_scene_item_reuse
    ON komari_memory_scene_item(scene_id, content_hash)
    """,
    """
    CREATE TABLE IF NOT EXISTS komari_memory_scene_runtime (
        id INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
        active_set_id BIGINT REFERENCES komari_memory_scene_set(id),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    INSERT INTO komari_memory_scene_runtime (id, active_set_id)
    VALUES (1, NULL)
    ON CONFLICT (id) DO NOTHING
    """,
)


@dataclass(frozen=True)
class SceneTemplateItem:
    """标准化后的 scene 条目。"""

    scene_key: str
    scene_type: str
    content_text: str
    enabled: bool
    order_index: int
    content_hash: str


@dataclass(frozen=True)
class SceneTemplatePayload:
    """标准化模板载荷。"""

    source_hash: str
    items: list[SceneTemplateItem]


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
        "--env-file",
        type=Path,
        default=_DEFAULT_ENV_PATH,
        help="dotenv 配置文件路径，默认读取项目根目录 .env",
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


def _load_dotenv_file(env_path: Path) -> None:
    """加载最小 dotenv 配置，且不覆盖已有环境变量。"""
    if not env_path.exists():
        print(f"dotenv 文件不存在，跳过: {env_path}")  # noqa: T201
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.startswith("export "):
            stripped = stripped.removeprefix("export ").strip()

        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _resolve_dsn() -> str:
    """读取 nonebot-plugin-orm 权威数据库连接串（SQLALCHEMY_DATABASE_URL）。"""
    dsn = os.getenv("SQLALCHEMY_DATABASE_URL")
    if not dsn:
        msg = (
            "未配置 SQLALCHEMY_DATABASE_URL，"
            "请通过环境变量或 dotenv 设置 nonebot-plugin-orm 的连接串"
        )
        raise RuntimeError(msg)
    return dsn


async def _create_pool_from_dsn(dsn: str) -> asyncpg.Pool:
    """创建脚本专用 PostgreSQL 连接池。"""
    return await asyncpg.create_pool(dsn=dsn, command_timeout=60)


def _compute_text_hash(text: str) -> str:
    """计算文本哈希。"""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def _compute_source_hash(payload: dict[str, Any]) -> str:
    """计算模板源哈希。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalize_fixed_candidates(raw_fixed: object) -> dict[str, str]:
    """标准化 fixed_candidates。"""
    if not isinstance(raw_fixed, dict):
        msg = "fixed_candidates 必须是对象"
        raise TypeError(msg)

    normalized: dict[str, str] = {}
    for key, value in raw_fixed.items():
        key_str = str(key).strip()
        value_str = str(value).strip()
        if not key_str or not value_str:
            continue
        normalized[key_str] = value_str

    if not normalized:
        msg = "fixed_candidates 不能为空"
        raise ValueError(msg)
    return normalized


def _normalize_general_scenes(raw_scenes: object) -> list[dict[str, str]]:
    """标准化 general_scenes。"""
    if not isinstance(raw_scenes, list):
        msg = "general_scenes 必须是数组"
        raise TypeError(msg)

    normalized: list[dict[str, str]] = []
    for item in raw_scenes:
        if not isinstance(item, dict):
            continue
        scene_id = str(item.get("id", "")).strip()
        scene_text = str(item.get("text", "")).strip()
        if not scene_id or not scene_text:
            continue
        normalized.append({"id": scene_id, "text": scene_text})

    if not normalized:
        msg = "general_scenes 不能为空"
        raise ValueError(msg)
    return normalized


def _load_scene_template(source_path: Path) -> SceneTemplatePayload:
    """读取 YAML 并输出标准化 scene 条目。"""
    path = source_path if source_path.is_absolute() else source_path.resolve()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as e:
        msg = f"读取 scene 模板失败: {path}"
        raise RuntimeError(msg) from e
    except yaml.YAMLError as e:
        msg = f"scene 模板 YAML 解析失败: {path}"
        raise RuntimeError(msg) from e

    if not isinstance(raw, dict):
        msg = "scene 模板根节点必须是对象"
        raise TypeError(msg)

    fixed_candidates = _normalize_fixed_candidates(raw.get("fixed_candidates", {}))
    general_scenes = _normalize_general_scenes(raw.get("general_scenes", []))
    items: list[SceneTemplateItem] = []
    order = 0

    for key, content in fixed_candidates.items():
        items.append(
            SceneTemplateItem(
                scene_key=key,
                scene_type="fixed",
                content_text=content,
                enabled=True,
                order_index=order,
                content_hash=_compute_text_hash(content),
            )
        )
        order += 1

    for scene in general_scenes:
        content = scene["text"]
        items.append(
            SceneTemplateItem(
                scene_key=scene["id"],
                scene_type="general",
                content_text=content,
                enabled=True,
                order_index=order,
                content_hash=_compute_text_hash(content),
            )
        )
        order += 1

    normalized_payload = {
        "fixed_candidates": fixed_candidates,
        "general_scenes": general_scenes,
        "items": [
            {
                "scene_key": item.scene_key,
                "scene_type": item.scene_type,
                "content_hash": item.content_hash,
                "enabled": item.enabled,
                "order_index": item.order_index,
            }
            for item in items
        ],
    }
    return SceneTemplatePayload(
        source_hash=_compute_source_hash(normalized_payload),
        items=items,
    )


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


async def _ensure_schema(pool: asyncpg.Pool) -> None:
    """确保 scene 持久化相关表结构存在。"""
    async with pool.acquire() as conn:
        for statement in _SCENE_SCHEMA_STATEMENTS:
            await conn.execute(statement)


async def _list_scenes(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    """列出 scene 内容表记录。"""
    async with pool.acquire() as conn:
        table_exists = await conn.fetchval("SELECT to_regclass('komari_decision_scenes')")
        if table_exists is None:
            return []
        rows = await conn.fetch(
            """
            SELECT id, scene_key, scene_type, content_text, content_hash,
                   enabled, order_index, created_at, updated_at
            FROM komari_decision_scenes
            ORDER BY order_index ASC, id ASC
            """
        )
    return [dict(row) for row in rows]


async def _run() -> None:
    args = _parse_args()
    _load_dotenv_file(args.env_file)
    payload = _load_scene_template(args.source_path)
    _validate_items(payload.items)

    pool = await _create_pool_from_dsn(_resolve_dsn())
    try:
        existing_rows = await _list_scenes(pool)
        stats = _build_stats(payload.items, existing_rows, payload.source_hash)
        if args.dry_run:
            _print_stats(stats, dry_run=True)
            return

        await _ensure_schema(pool)
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
