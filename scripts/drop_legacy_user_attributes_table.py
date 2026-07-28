"""显式删除旧版 user_attributes 表的一次性维护脚本。

该脚本不会由插件启动流程调用，需维护者在确认旧用户属性数据不再需要后手动执行。
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg

PROJECT_ROOT = Path(__file__).resolve().parents[1]

logger = logging.getLogger("drop_legacy_user_attributes_table")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


@dataclass(frozen=True)
class PostgresConfig:
    """脚本独立使用的最小 PostgreSQL 配置。"""

    pg_host: str
    pg_port: int
    pg_database: str
    pg_user: str
    pg_password: str
    pg_pool_min_size: int
    pg_pool_max_size: int


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


def _get_env_int(name: str, default: int) -> int:
    """读取整数环境变量。"""
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        msg = f"环境变量 {name} 必须是整数，当前值: {value!r}"
        raise ValueError(msg) from exc


def _load_postgres_config_from_env() -> PostgresConfig:
    """从当前进程环境变量读取脚本所需的 PostgreSQL 配置。"""
    min_size = max(1, _get_env_int("PG_POOL_MIN_SIZE", 2))
    max_size = max(min_size, _get_env_int("PG_POOL_MAX_SIZE", 5))
    return PostgresConfig(
        pg_host=os.getenv("PG_HOST", "localhost"),
        pg_port=_get_env_int("PG_PORT", 5432),
        pg_database=os.getenv("PG_DATABASE", "komari_bot"),
        pg_user=os.getenv("PG_USER", ""),
        pg_password=os.getenv("PG_PASSWORD", ""),
        pg_pool_min_size=min_size,
        pg_pool_max_size=max_size,
    )


async def _create_postgres_pool(config: PostgresConfig) -> asyncpg.Pool:
    """创建脚本专用 PostgreSQL 连接池。"""
    return await asyncpg.create_pool(
        host=config.pg_host,
        port=config.pg_port,
        database=config.pg_database,
        user=config.pg_user,
        password=config.pg_password,
        min_size=config.pg_pool_min_size,
        max_size=config.pg_pool_max_size,
        command_timeout=30,
    )


async def drop_legacy_table(pool: Any) -> None:
    """删除废弃的用户属性表。"""
    async with pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS user_attributes")
    logger.info("已执行旧表删除: DROP TABLE IF EXISTS user_attributes")


async def main_async() -> None:
    """连接 PostgreSQL 并删除旧表。"""
    _load_dotenv_file(PROJECT_ROOT / ".env")
    config = _load_postgres_config_from_env()
    pool = await _create_postgres_pool(config)
    try:
        await drop_legacy_table(pool)
    finally:
        await pool.close()
        logger.info("PostgreSQL 连接池已关闭")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
