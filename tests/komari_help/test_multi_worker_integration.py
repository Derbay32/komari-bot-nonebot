"""可选的 Komari Help 多 worker PostgreSQL 集成测试。

依赖已执行 alembic upgrade head 的迁移管理 schema（komari_help 为 nullable
VECTOR(维度) 列）；连接来源为 nonebot-plugin-orm 共享引擎租约。测试使用唯一
plugin_name 并清理写入的 help 行与扫描租约。
"""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import pytest

from komari_bot.db.pgvector_schema import get_vector_column_dimension
from komari_bot.plugins.komari_help.engine import HelpEngine

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")


def _configured_database_url() -> str:
    from nonebot import get_driver

    return str(getattr(get_driver().config, "sqlalchemy_database_url", "") or "")


def _same_database(left: str, right: str) -> bool:
    left_parsed = urlparse(left)
    right_parsed = urlparse(right)
    return (
        left_parsed.hostname == right_parsed.hostname
        and (left_parsed.port or 5432) == (right_parsed.port or 5432)
        and left_parsed.path == right_parsed.path
    )


def _asyncpg_url() -> str:
    return POSTGRES_URL.replace("postgresql+asyncpg://", "postgresql://")


async def _reset_shared_orm_engine() -> None:
    from nonebot import require

    require("nonebot_plugin_orm")
    import nonebot_plugin_orm as orm_module

    engines = getattr(orm_module, "_engines", None)
    if not engines:
        return
    for engine in list(engines.values()):
        with suppress(Exception):
            await engine.dispose()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_help_scan_lease_and_auto_sync_are_multi_worker_safe() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    from komari_bot.db.orm_connection import get_shared_orm_connection_pool

    run_id = uuid4().hex
    plugin_name = f"demo_plugin_{run_id}"
    worker_tokens = [f"worker-1-{run_id}", f"worker-2-{run_id}"]
    pool = get_shared_orm_connection_pool()
    admin = await asyncpg.connect(_asyncpg_url())
    try:
        dimension = await get_vector_column_dimension(
            pool,
            table_name="komari_help",
            column_name="embedding",
        )
        assert dimension is not None and dimension > 0

        first = HelpEngine()
        second = HelpEngine()
        first._pool = pool
        second._pool = pool

        assert await first.acquire_scan_lease(worker_tokens[0], lease_seconds=30) is True
        assert await second.acquire_scan_lease(worker_tokens[1], lease_seconds=30) is False
        assert await second.renew_scan_lease(worker_tokens[1], lease_seconds=30) is False
        assert await first.renew_scan_lease(worker_tokens[0], lease_seconds=30) is True
        await first.release_scan_lease(worker_tokens[0])
        assert await second.acquire_scan_lease(worker_tokens[1], lease_seconds=30) is True
        await second.release_scan_lease(worker_tokens[1])

        embedding_barrier = asyncio.Barrier(2)
        fake_embedding = [0.1] * dimension

        async def _embedding(_text: str) -> list[float]:
            await embedding_barrier.wait()
            return list(fake_embedding)

        first._get_embedding = _embedding  # type: ignore[method-assign]
        second._get_embedding = _embedding  # type: ignore[method-assign]
        results = await asyncio.gather(
            first.sync_auto_generated_help(
                plugin_name=plugin_name,
                title="演示插件",
                content="/demo help",
                keywords=["演示", "帮助"],
                rebuild_index=False,
            ),
            second.sync_auto_generated_help(
                plugin_name=plugin_name,
                title="演示插件",
                content="/demo help",
                keywords=["演示", "帮助"],
                rebuild_index=False,
            ),
        )

        async with pool.acquire() as connection:  # type: ignore[attr-defined]
            auto_rows = await connection.fetch(
                """
                SELECT plugin_name, title, content
                FROM komari_help
                WHERE plugin_name = $1
                  AND is_auto_generated = TRUE
                """,
                plugin_name,
            )
        assert sorted(results) == [False, True]
        assert len(auto_rows) == 1
        assert auto_rows[0]["title"] == "演示插件"
    finally:
        await admin.execute(
            "DELETE FROM komari_help WHERE plugin_name = $1",
            plugin_name,
        )
        await admin.close()
        await _reset_shared_orm_engine()
