"""Migrate user_data from SQLite to PostgreSQL.

Usage:
python tools/migrate_user_data_sqlite_to_pg.py
python tools/migrate_user_data_sqlite_to_pg.py --sqlite-path user_data.db
python tools/migrate_user_data_sqlite_to_pg.py --config-path config/config_manager/user_data_config.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if TYPE_CHECKING:
    from komari_bot.plugins.user_data.config_schema import DynamicConfigSchema

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("migrate_user_data")


def load_config(config_path: Path) -> "DynamicConfigSchema":
    """Load user_data config from config_manager JSON file."""
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")  # noqa: TRY003

    data = json.loads(config_path.read_text(encoding="utf-8"))
    from komari_bot.plugins.user_data.config_schema import DynamicConfigSchema

    return DynamicConfigSchema(**data)


def read_sqlite_rows(sqlite_path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read source rows from SQLite."""
    if not sqlite_path.exists():
        raise FileNotFoundError(f"SQLite 文件不存在: {sqlite_path}")  # noqa: TRY003

    conn = sqlite3.connect(str(sqlite_path))
    conn.row_factory = sqlite3.Row
    try:
        user_attributes: list[dict[str, Any]] = []
        user_favorability: list[dict[str, Any]] = []

        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        if "user_attributes" in tables:
            rows = conn.execute(
                """
                SELECT user_id, attribute_name, attribute_value, created_at, updated_at
                FROM user_attributes
                """
            ).fetchall()
            user_attributes = [dict(row) for row in rows]
        else:
            logger.warning("SQLite 缺少 user_attributes 表，跳过该表迁移")

        if "user_favorability" in tables:
            rows = conn.execute(
                """
                SELECT user_id, daily_favor, cumulative_favor, last_updated
                FROM user_favorability
                """
            ).fetchall()
            user_favorability = [dict(row) for row in rows]
        else:
            logger.warning("SQLite 缺少 user_favorability 表，跳过该表迁移")

        return user_attributes, user_favorability
    finally:
        conn.close()


async def ensure_pg_schema(pool: Any) -> None:
    """Create target schema in PostgreSQL if missing."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_attributes (
                id BIGSERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                attribute_name TEXT NOT NULL,
                attribute_value TEXT,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, attribute_name)
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_favorability (
                user_id TEXT NOT NULL,
                daily_favor INTEGER DEFAULT 0,
                cumulative_favor INTEGER DEFAULT 0,
                last_updated DATE NOT NULL,
                PRIMARY KEY (user_id, last_updated)
            )
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_user_attributes_composite
            ON user_attributes(user_id, attribute_name)
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_user_favorability_composite
            ON user_favorability(user_id, last_updated)
            """
        )


async def migrate_to_postgres(
    config: "DynamicConfigSchema",
    user_attributes: list[dict[str, Any]],
    user_favorability: list[dict[str, Any]],
) -> None:
    """Migrate rows into PostgreSQL."""
    try:
        from komari_bot.common.postgres import create_postgres_pool
    except ModuleNotFoundError as exc:
        msg = (
            "缺少依赖 asyncpg，请先安装项目依赖后再运行迁移脚本。"
            "例如: pip install -r requirements.txt"
        )
        raise ModuleNotFoundError(msg) from exc

    pool = await create_postgres_pool(config)
    try:
        await ensure_pg_schema(pool)

        async with pool.acquire() as conn, conn.transaction():
            if user_attributes:
                await conn.executemany(
                    """
                    INSERT INTO user_attributes
                    (user_id, attribute_name, attribute_value, created_at, updated_at)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (user_id, attribute_name)
                    DO UPDATE SET
                        attribute_value = EXCLUDED.attribute_value,
                        updated_at = COALESCE(EXCLUDED.updated_at, CURRENT_TIMESTAMP)
                    """,
                    [
                        (
                            row["user_id"],
                            row["attribute_name"],
                            row.get("attribute_value"),
                            row.get("created_at"),
                            row.get("updated_at"),
                        )
                        for row in user_attributes
                    ],
                )

            if user_favorability:
                await conn.executemany(
                    """
                    INSERT INTO user_favorability
                    (user_id, daily_favor, cumulative_favor, last_updated)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (user_id, last_updated)
                    DO UPDATE SET
                        daily_favor = EXCLUDED.daily_favor,
                        cumulative_favor = EXCLUDED.cumulative_favor
                    """,
                    [
                        (
                            row["user_id"],
                            int(row.get("daily_favor") or 0),
                            int(row.get("cumulative_favor") or 0),
                            row["last_updated"],
                        )
                        for row in user_favorability
                    ],
                )
    finally:
        await pool.close()


async def main_async(sqlite_path: Path, config_path: Path) -> None:
    config = load_config(config_path)
    if not config.pg_user or not config.pg_password:
        raise ValueError("pg_user/pg_password 未配置，无法迁移")  # noqa: TRY003

    user_attributes, user_favorability = read_sqlite_rows(sqlite_path)
    logger.info("读取 SQLite 完成: attributes=%d, favorability=%d", len(user_attributes), len(user_favorability))

    await migrate_to_postgres(config, user_attributes, user_favorability)
    logger.info("迁移完成")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate user_data SQLite -> PostgreSQL")
    parser.add_argument(
        "--sqlite-path",
        default="user_data.db",
        help="SQLite 源文件路径，默认 user_data.db",
    )
    parser.add_argument(
        "--config-path",
        default="config/config_manager/user_data_config.json",
        help="user_data config_manager JSON 路径",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(
        main_async(
            sqlite_path=Path(args.sqlite_path),
            config_path=Path(args.config_path),
        )
    )


if __name__ == "__main__":
    main()
