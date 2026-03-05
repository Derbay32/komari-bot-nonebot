"""将 user_data 从 SQLite 迁移到 PostgreSQL。

用法：
python tools/migrate_user_data_sqlite_to_pg.py
python tools/migrate_user_data_sqlite_to_pg.py --sqlite-path user_data.db
python tools/migrate_user_data_sqlite_to_pg.py --config-path config/config_manager/user_data_config.json
python tools/migrate_user_data_sqlite_to_pg.py --db-config-path config/config_manager/database_config.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if TYPE_CHECKING:
    from komari_bot.common.database_config import DatabaseConfigSchema


@dataclass
class UserDataDbOverrideConfig:
    """user_data 中与数据库相关的可选覆盖字段。"""

    pg_host: str | None = None
    pg_port: int | None = None
    pg_database: str | None = None
    pg_user: str | None = None
    pg_password: str | None = None
    pg_pool_min_size: int | None = None
    pg_pool_max_size: int | None = None


def _read_optional_int(data: dict[str, Any], key: str) -> int | None:
    value = data.get(key)
    return None if value is None else int(value)


def _parse_datetime_value(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time(), tzinfo=UTC)

    text = str(value).strip()
    if not text:
        return None

    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        for fmt in (
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
        ):
            try:
                parsed = datetime.strptime(text, fmt)  # noqa: DTZ007
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"无法解析为 datetime: {value}")  # noqa: TRY003

    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_date_value(value: Any) -> date | None:
    parsed: date | None = None
    if isinstance(value, datetime):
        parsed = value.date()
    elif isinstance(value, date):
        parsed = value
    elif value is not None:
        text = str(value).strip()
        if text:
            try:
                parsed = date.fromisoformat(text)
            except ValueError:
                parsed_dt = _parse_datetime_value(text)
                parsed = parsed_dt.date() if parsed_dt else None
    return parsed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("migrate_user_data")


def load_user_data_db_override(config_path: Path) -> UserDataDbOverrideConfig:
    """从 user_data 配置中读取数据库覆盖字段。"""
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")  # noqa: TRY003

    data = json.loads(config_path.read_text(encoding="utf-8"))
    return UserDataDbOverrideConfig(
        pg_host=data.get("pg_host"),
        pg_port=_read_optional_int(data, "pg_port"),
        pg_database=data.get("pg_database"),
        pg_user=data.get("pg_user"),
        pg_password=data.get("pg_password"),
        pg_pool_min_size=_read_optional_int(data, "pg_pool_min_size"),
        pg_pool_max_size=_read_optional_int(data, "pg_pool_max_size"),
    )


def resolve_db_config(
    user_data_db_override: UserDataDbOverrideConfig,
    db_config_path: Path,
) -> "DatabaseConfigSchema":
    """解析最终 PostgreSQL 配置（共享配置 + user_data 覆盖）。"""
    from komari_bot.common.database_config import (
        DatabaseConfigSchema,
        load_database_config_from_file,
        merge_database_config,
    )

    shared = (
        load_database_config_from_file(db_config_path)
        if db_config_path.exists()
        else DatabaseConfigSchema()
    )
    return merge_database_config(shared, user_data_db_override)


def read_sqlite_rows(sqlite_path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """从 SQLite 读取源数据。"""
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
    """确保 PostgreSQL 目标表结构存在。"""
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
    config: "DatabaseConfigSchema",
    user_attributes: list[dict[str, Any]],
    user_favorability: list[dict[str, Any]],
) -> None:
    """将数据写入 PostgreSQL。"""
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
                attribute_rows = [
                    (
                        row["user_id"],
                        row["attribute_name"],
                        row.get("attribute_value"),
                        _parse_datetime_value(row.get("created_at")),
                        _parse_datetime_value(row.get("updated_at")),
                    )
                    for row in user_attributes
                ]
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
                    attribute_rows,
                )

            if user_favorability:
                favor_rows: list[tuple[str, int, int, date]] = []
                skipped_rows = 0
                for row in user_favorability:
                    last_updated = _parse_date_value(row.get("last_updated"))
                    if last_updated is None:
                        skipped_rows += 1
                        continue
                    favor_rows.append(
                        (
                            row["user_id"],
                            int(row.get("daily_favor") or 0),
                            int(row.get("cumulative_favor") or 0),
                            last_updated,
                        )
                    )

                if skipped_rows:
                    logger.warning(
                        "user_favorability 有 %d 条记录缺少 last_updated，已跳过",
                        skipped_rows,
                    )

                if not favor_rows:
                    return

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
                    favor_rows,
                )
    finally:
        await pool.close()


async def main_async(sqlite_path: Path, config_path: Path, db_config_path: Path) -> None:
    user_data_db_override = load_user_data_db_override(config_path)
    db_config = resolve_db_config(user_data_db_override, db_config_path)
    if not db_config.pg_user or not db_config.pg_password:
        raise ValueError("pg_user/pg_password 未配置，无法迁移")  # noqa: TRY003

    user_attributes, user_favorability = read_sqlite_rows(sqlite_path)
    logger.info(
        "读取 SQLite 完成: 用户属性=%d, 好感度=%d",
        len(user_attributes),
        len(user_favorability),
    )

    await migrate_to_postgres(db_config, user_attributes, user_favorability)
    logger.info("迁移完成")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="迁移 user_data：SQLite -> PostgreSQL")
    parser.add_argument(
        "--sqlite-path",
        default="user_data.db",
        help="SQLite 源文件路径，默认 user_data.db",
    )
    parser.add_argument(
        "--config-path",
        default="config/config_manager/user_data_config.json",
        help="user_data 的 config_manager JSON 路径",
    )
    parser.add_argument(
        "--db-config-path",
        default="config/config_manager/database_config.json",
        help="共享 database_config JSON 路径",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(
        main_async(
            sqlite_path=Path(args.sqlite_path),
            config_path=Path(args.config_path),
            db_config_path=Path(args.db_config_path),
        )
    )


if __name__ == "__main__":
    main()
