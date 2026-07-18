"""进程内共享 PostgreSQL 连接池测试。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest

from komari_bot.common import postgres as postgres_module
from komari_bot.common.database_config import DatabaseConfigSchema
from komari_bot.common.postgres import (
    PostgresPoolBudgetExceededError,
    create_postgres_pool,
    get_postgres_pool_stats,
)

if TYPE_CHECKING:
    import asyncpg


class _PhysicalPool:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


def _config(**overrides: object) -> DatabaseConfigSchema:
    values: dict[str, object] = {
        "pg_host": "127.0.0.1",
        "pg_port": 5432,
        "pg_database": "komari_test",
        "pg_user": "komari",
        "pg_password": "test-password",
        "pg_pool_min_size": 1,
        "pg_pool_max_size": 3,
        "pg_pool_process_budget": 6,
    }
    values.update(overrides)
    return DatabaseConfigSchema.model_validate(values)


@pytest.mark.asyncio
async def test_identical_pool_requests_share_one_physical_pool_until_last_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    physical_pool = _PhysicalPool()
    create_calls: list[dict[str, Any]] = []

    async def _fake_create_pool(**kwargs: Any) -> asyncpg.Pool:
        create_calls.append(kwargs)
        return cast("asyncpg.Pool", physical_pool)

    monkeypatch.setattr(postgres_module.asyncpg, "create_pool", _fake_create_pool)

    first = await create_postgres_pool(_config())
    second = await create_postgres_pool(_config())

    assert len(create_calls) == 1
    assert get_postgres_pool_stats() == {
        "event_loop_count": 1,
        "physical_pool_count": 1,
        "lease_count": 2,
        "reserved_max_connections": 3,
    }

    await first.close()
    assert physical_pool.close_calls == 0
    assert get_postgres_pool_stats()["lease_count"] == 1

    await second.close()
    assert physical_pool.close_calls == 1
    assert get_postgres_pool_stats()["physical_pool_count"] == 0
    assert get_postgres_pool_stats()["reserved_max_connections"] == 0


@pytest.mark.asyncio
async def test_distinct_physical_pool_is_rejected_before_process_budget_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    physical_pool = _PhysicalPool()

    async def _fake_create_pool(**_kwargs: Any) -> asyncpg.Pool:
        return cast("asyncpg.Pool", physical_pool)

    monkeypatch.setattr(postgres_module.asyncpg, "create_pool", _fake_create_pool)
    first = await create_postgres_pool(
        _config(pg_pool_max_size=3, pg_pool_process_budget=5)
    )

    with pytest.raises(PostgresPoolBudgetExceededError, match="进程上限 5"):
        await create_postgres_pool(
            _config(
                pg_database="second_database",
                pg_pool_max_size=3,
                pg_pool_process_budget=5,
            )
        )

    assert get_postgres_pool_stats()["physical_pool_count"] == 1
    await first.close()
    assert physical_pool.close_calls == 1


@pytest.mark.asyncio
async def test_failed_physical_pool_creation_releases_reserved_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fail_create_pool(**_kwargs: Any) -> asyncpg.Pool:
        raise OSError("连接失败")

    monkeypatch.setattr(postgres_module.asyncpg, "create_pool", _fail_create_pool)

    with pytest.raises(OSError, match="连接失败"):
        await create_postgres_pool(_config())

    assert get_postgres_pool_stats()["physical_pool_count"] == 0
    assert get_postgres_pool_stats()["reserved_max_connections"] == 0
