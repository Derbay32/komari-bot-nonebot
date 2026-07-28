"""将 komari_search 插件配置从 v1.0 键名迁移到 v2.0。

JSONB 键重命名：
- ``tavily_api_key`` → ``search_api_key``
- ``search_depth`` → ``tavily_search_depth``
- ``include_answer`` → ``tavily_include_answer``

迁移幂等：以旧键是否全部清除为判据。新代码启动时 config_manager 会自动
把新 Schema 默认值（含空 ``search_api_key``）合并写回 PG，但保留旧键，
因此不能以新键存在与否判断是否已迁移。转移旧值时仅在新键为空时覆盖，
避免冲掉运维已通过管理 API 录入的新键。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from komari_bot.common.database_config import load_database_config_from_env
from komari_bot.common.postgres import create_postgres_pool

logger = logging.getLogger("migrate_search_config_v2")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

_PLUGIN_NAME = "komari_search"
_TARGET_VERSION = "2.0"
_KEY_RENAMES: dict[str, str] = {
    "tavily_api_key": "search_api_key",
    "search_depth": "tavily_search_depth",
    "include_answer": "tavily_include_answer",
}


def _load_dotenv_file(env_path: Path) -> None:
    """加载最小 dotenv 配置，且不覆盖已有环境变量。"""
    if not env_path.exists():
        logger.info("dotenv 文件不存在，跳过: %s", env_path)
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue

        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _migrate_config_data(config_data: dict[str, Any]) -> dict[str, Any] | None:
    """返回迁移后的配置副本；无需迁移时返回 None。"""
    if not any(old_key in config_data for old_key in _KEY_RENAMES):
        return None

    migrated = dict(config_data)
    for old_key, new_key in _KEY_RENAMES.items():
        if old_key not in migrated:
            continue
        # 自动同步可能已写入空的新键，仅在新键为空时转移旧值
        if not migrated.get(new_key):
            migrated[new_key] = migrated[old_key]
        del migrated[old_key]
    migrated["version"] = _TARGET_VERSION
    return migrated


async def migrate_search_config(pool: Any, *, dry_run: bool) -> dict[str, int]:
    """执行 komari_search 配置键名迁移。"""
    stats = {"success": 0, "skipped": 0, "missing": 0}

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT config_data, version
            FROM komari_plugin_configs
            WHERE plugin_name = $1
            """,
            _PLUGIN_NAME,
        )
        if row is None:
            logger.info("未找到 %s 配置行，无需迁移", _PLUGIN_NAME)
            stats["missing"] += 1
            return stats

        raw_data = row["config_data"]
        config_data = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
        if not isinstance(config_data, dict):
            logger.warning("%s 配置行不是 JSON 对象，跳过", _PLUGIN_NAME)
            stats["skipped"] += 1
            return stats

        migrated = _migrate_config_data(config_data)
        if migrated is None:
            logger.info("%s 配置旧键已全部清除，跳过（幂等）", _PLUGIN_NAME)
            stats["skipped"] += 1
            return stats

        renamed_keys = [
            f"{old} -> {new}"
            for old, new in _KEY_RENAMES.items()
            if old in config_data
        ]
        if dry_run:
            logger.info(
                "[dry-run] 将迁移 %s: %s; version -> %s",
                _PLUGIN_NAME,
                ", ".join(renamed_keys) or "仅版本号",
                _TARGET_VERSION,
            )
            stats["success"] += 1
            return stats

        await conn.execute(
            """
            UPDATE komari_plugin_configs
            SET config_data = $2::jsonb,
                version = $3,
                updated_at = CURRENT_TIMESTAMP
            WHERE plugin_name = $1
            """,
            _PLUGIN_NAME,
            json.dumps(migrated, ensure_ascii=False),
            _TARGET_VERSION,
        )
        logger.info(
            "已迁移 %s: %s; version -> %s",
            _PLUGIN_NAME,
            ", ".join(renamed_keys) or "仅版本号",
            _TARGET_VERSION,
        )
        stats["success"] += 1

    return stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="将 komari_search 插件配置键名迁移到 v2.0"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将要执行的迁移，不写入数据库",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=PROJECT_ROOT / ".env",
        help="dotenv 配置文件路径，默认读取项目根目录 .env",
    )
    return parser.parse_args()


async def main_async() -> None:
    args = parse_args()
    _load_dotenv_file(args.env_file)
    config = load_database_config_from_env()
    pool = await create_postgres_pool(config)
    try:
        stats = await migrate_search_config(pool, dry_run=args.dry_run)
        logger.info(
            "迁移统计: success=%s skipped=%s missing=%s",
            stats["success"],
            stats["skipped"],
            stats["missing"],
        )
    finally:
        await pool.close()


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
