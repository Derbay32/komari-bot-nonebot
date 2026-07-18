"""可选的 Komari Help 多 worker PostgreSQL 集成测试。"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import asyncpg
import pytest

from komari_bot.common.vector_storage_schema import build_help_schema_statements
from komari_bot.plugins.komari_help.engine import HelpEngine

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_help_scan_lease_and_auto_sync_are_multi_worker_safe() -> None:
    schema_name = f"help_multi_worker_{uuid4().hex}"
    admin = await asyncpg.connect(POSTGRES_URL)
    pool: asyncpg.Pool | None = None
    try:
        await admin.execute(f"CREATE SCHEMA {schema_name}")
        pool = await asyncpg.create_pool(
            POSTGRES_URL,
            min_size=1,
            max_size=4,
            server_settings={"search_path": schema_name},
        )
        schema_statements = build_help_schema_statements(3)
        async with pool.acquire() as connection:
            # 该集成夹具不要求测试机安装 pgvector；同步逻辑只需验证并发写入，
            # 因此用 TEXT 承接序列化向量，并执行生产代码生成的并发约束 DDL。
            await connection.execute(
                """
                CREATE TABLE komari_help (
                    id SERIAL PRIMARY KEY,
                    category TEXT NOT NULL DEFAULT 'other',
                    plugin_name TEXT,
                    keywords TEXT[] NOT NULL DEFAULT '{}',
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    notes TEXT,
                    is_auto_generated BOOLEAN NOT NULL DEFAULT FALSE,
                    embedding TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            for marker in (
                "CREATE TABLE IF NOT EXISTS komari_help_scan_leases",
                "WITH ranked_auto_help AS",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_komari_help_auto_plugin",
            ):
                statement = next(
                    item for item in schema_statements if marker in item
                )
                await connection.execute(statement)

        first = HelpEngine()
        second = HelpEngine()
        first._pool = pool
        second._pool = pool

        assert await first.acquire_scan_lease("worker-1", lease_seconds=30) is True
        assert await second.acquire_scan_lease("worker-2", lease_seconds=30) is False
        assert await second.renew_scan_lease("worker-2", lease_seconds=30) is False
        assert await first.renew_scan_lease("worker-1", lease_seconds=30) is True
        await first.release_scan_lease("worker-1")
        assert await second.acquire_scan_lease("worker-2", lease_seconds=30) is True
        await second.release_scan_lease("worker-2")

        embedding_barrier = asyncio.Barrier(2)

        async def _embedding(_text: str) -> list[float]:
            await embedding_barrier.wait()
            return [0.1, 0.2, 0.3]

        first._get_embedding = _embedding  # type: ignore[method-assign]
        second._get_embedding = _embedding  # type: ignore[method-assign]
        results = await asyncio.gather(
            first.sync_auto_generated_help(
                plugin_name="demo_plugin",
                title="演示插件",
                content="/demo help",
                keywords=["演示", "帮助"],
                rebuild_index=False,
            ),
            second.sync_auto_generated_help(
                plugin_name="demo_plugin",
                title="演示插件",
                content="/demo help",
                keywords=["演示", "帮助"],
                rebuild_index=False,
            ),
        )

        async with pool.acquire() as connection:
            auto_rows = await connection.fetch(
                """
                SELECT plugin_name, title, content
                FROM komari_help
                WHERE plugin_name = 'demo_plugin'
                  AND is_auto_generated = TRUE
                """
            )
        assert sorted(results) == [False, True]
        assert len(auto_rows) == 1
        assert auto_rows[0]["title"] == "演示插件"
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE")
        await admin.close()
