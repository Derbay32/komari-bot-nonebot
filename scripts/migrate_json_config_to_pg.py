"""将旧版 JSON 插件配置一次性导入 PostgreSQL 配置表。

脚本只执行 upsert，不删除本地 JSON 文件。``database_config.json`` 会被跳过，
因为 PG / Redis 引导配置必须来自 dotenv / 环境变量。
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

import asyncpg

logger = logging.getLogger("migrate_json_config_to_pg")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

_CONFIG_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS komari_plugin_configs (
    plugin_name VARCHAR(128) PRIMARY KEY,
    schema_name VARCHAR(128) NOT NULL,
    config_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    version VARCHAR(32) NOT NULL DEFAULT '1.0',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

_CONFIG_TABLE_UPDATED_AT_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_komari_plugin_configs_updated_at
    ON komari_plugin_configs (updated_at DESC);
"""


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


def _resolve_plugin_name(config_path: Path) -> str:
    stem = config_path.stem
    return stem.removesuffix("_config")


def _load_json_config(config_path: Path) -> dict[str, Any]:
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        msg = f"配置文件顶层必须是对象: {config_path}"
        raise TypeError(msg)
    return data


async def _upsert_config(
    conn: Any,
    *,
    plugin_name: str,
    schema_name: str,
    config_data: dict[str, Any],
    version: str,
) -> None:
    row = await conn.fetchrow(
        """
        INSERT INTO komari_plugin_configs (
            plugin_name,
            schema_name,
            config_data,
            version
        )
        VALUES ($1, $2, $3::jsonb, $4)
        ON CONFLICT (plugin_name) DO UPDATE SET
            schema_name = EXCLUDED.schema_name,
            config_data = EXCLUDED.config_data,
            version = EXCLUDED.version,
            updated_at = CURRENT_TIMESTAMP
        RETURNING plugin_name
        """,
        plugin_name,
        schema_name,
        json.dumps(config_data, ensure_ascii=False),
        version,
    )
    if row is None:
        msg = f"配置写入后未返回记录: {plugin_name}"
        raise RuntimeError(msg)


async def migrate_config_dir(config_dir: Path, pool: Any) -> dict[str, int]:
    """迁移指定目录下的旧版插件 JSON 配置。"""
    stats = {"success": 0, "skipped": 0, "failed": 0}

    async with pool.acquire() as conn:
        await conn.execute(_CONFIG_TABLE_DDL)
        await conn.execute(_CONFIG_TABLE_UPDATED_AT_INDEX_DDL)

        for config_path in sorted(config_dir.glob("*_config.json")):
            if config_path.name == "database_config.json":
                logger.info("跳过数据库引导配置: %s", config_path)
                stats["skipped"] += 1
                continue

            plugin_name = _resolve_plugin_name(config_path)
            try:
                data = _load_json_config(config_path)
                await _upsert_config(
                    conn,
                    plugin_name=plugin_name,
                    schema_name="LegacyJsonConfig",
                    config_data=data,
                    version=str(data.get("version", "1.0")),
                )
            except Exception:
                logger.exception("迁移失败: %s", config_path)
                stats["failed"] += 1
                continue

            logger.info("已导入配置: %s -> %s", config_path, plugin_name)
            stats["success"] += 1

    return stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="将旧版 JSON 插件配置导入 PostgreSQL")
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=PROJECT_ROOT / "config/config_manager",
        help="旧版插件 JSON 配置目录",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=PROJECT_ROOT / ".env",
        help="dotenv 配置文件路径，默认读取项目根目录 .env",
    )
    return parser.parse_args()


def _resolve_dsn() -> str:
    """读取 nonebot-plugin-orm 权威数据库连接串（SQLALCHEMY_DATABASE_URL）。"""
    dsn = os.environ.get("SQLALCHEMY_DATABASE_URL", "")
    if not dsn:
        msg = (
            "未配置 SQLALCHEMY_DATABASE_URL，"
            "请通过环境变量设置 nonebot-plugin-orm 的连接串"
        )
        raise RuntimeError(msg)
    return dsn


async def main_async() -> None:
    args = parse_args()
    _load_dotenv_file(args.env_file)
    pool = await asyncpg.create_pool(dsn=_resolve_dsn(), command_timeout=30)
    try:
        stats = await migrate_config_dir(args.config_dir, pool)
        logger.info(
            "迁移统计: success=%s skipped=%s failed=%s",
            stats["success"],
            stats["skipped"],
            stats["failed"],
        )
    finally:
        await pool.close()


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
