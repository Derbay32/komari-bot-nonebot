"""可选的好感度 operation 幂等 PostgreSQL 集成测试。"""

from __future__ import annotations

import asyncio
import os
from typing import Any, cast
from uuid import uuid4

import asyncpg
import pytest

from komari_bot.plugins.user_data.config_schema import DynamicConfigSchema
from komari_bot.plugins.user_data.database import UserDataDB

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_adjustment_operation_is_applied_once_under_concurrency() -> None:
    schema_name = f"favorability_ledger_{uuid4().hex}"
    admin = await asyncpg.connect(POSTGRES_URL)
    pool: asyncpg.Pool | None = None
    try:
        await admin.execute(f"CREATE SCHEMA {schema_name}")
        pool = await asyncpg.create_pool(
            POSTGRES_URL,
            min_size=1,
            max_size=2,
            server_settings={"search_path": schema_name},
        )
        database = UserDataDB(DynamicConfigSchema(initial_favorability=100))
        database._pool = cast("Any", pool)
        await database._create_tables(pool)

        first, duplicate = await asyncio.gather(
            database.adjust_user_favorability(
                "user-1",
                5,
                operation_id="reply-operation-1:favorability",
            ),
            database.adjust_user_favorability(
                "user-1",
                5,
                operation_id="reply-operation-1:favorability",
            ),
        )

        assert first.before == duplicate.before == 100
        assert first.after == duplicate.after == 105
        async with pool.acquire() as connection:
            score = await connection.fetchval(
                """
                SELECT favorability
                FROM user_favorability
                WHERE user_id = 'user-1'
                """
            )
            ledger_count = await connection.fetchval(
                """
                SELECT COUNT(*)
                FROM user_favorability_adjustment_ledger
                """
            )
        assert score == 105
        assert ledger_count == 1
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE")
        await admin.close()
