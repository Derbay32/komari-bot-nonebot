"""角色名绑定 PostgreSQL 可选集成测试。"""

from __future__ import annotations

import os
from typing import Any, cast
from uuid import uuid4

import asyncpg
import pytest

from komari_bot.plugins.character_binding.database import CharacterBindingDB

POSTGRES_URL = os.getenv("POSTGRES_URL") or os.getenv(
    "KOMARI_TEST_POSTGRES_URL",
    "",
)


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_schema_upsert_and_delete_are_idempotent() -> None:
    schema_name = f"character_binding_{uuid4().hex}"
    admin = await asyncpg.connect(POSTGRES_URL)
    pool: asyncpg.Pool | None = None
    try:
        await admin.execute(f"CREATE SCHEMA {schema_name}")
        pool = await asyncpg.create_pool(
            POSTGRES_URL,
            min_size=1,
            max_size=1,
            server_settings={"search_path": schema_name},
        )
        database = CharacterBindingDB()
        database._pool = cast("Any", pool)

        await database._create_table(pool)
        await database._create_table(pool)
        await database.upsert("42", "泉此方")
        await database.upsert("42", "柊镜")

        assert await database.load_all() == {"42": "柊镜"}
        assert await database.delete("42") is True
        assert await database.delete("42") is False
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE")
        await admin.close()
