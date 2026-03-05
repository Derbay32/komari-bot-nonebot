"""Shared PostgreSQL connection helpers."""

from __future__ import annotations

from typing import Protocol

import asyncpg


class PostgresConfig(Protocol):
    """Minimal PostgreSQL config contract."""

    pg_host: str
    pg_port: int
    pg_database: str
    pg_user: str
    pg_password: str


def _resolve_pool_size(config: object) -> tuple[int, int]:
    min_size = int(getattr(config, "pg_pool_min_size", 2))
    max_size = int(getattr(config, "pg_pool_max_size", 5))
    min_size = max(1, min_size)
    max_size = max(min_size, max_size)
    return min_size, max_size


async def create_postgres_pool(
    config: PostgresConfig,
    *,
    command_timeout: float = 30,
) -> asyncpg.Pool:
    """Create a shared asyncpg connection pool from common config fields."""
    min_size, max_size = _resolve_pool_size(config)
    return await asyncpg.create_pool(
        host=config.pg_host,
        port=config.pg_port,
        database=config.pg_database,
        user=config.pg_user,
        password=config.pg_password,
        min_size=min_size,
        max_size=max_size,
        command_timeout=float(command_timeout),
    )
