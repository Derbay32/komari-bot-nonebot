"""共享引擎 asyncpg 兼容连接适配的 PostgreSQL 集成测试。

本文件取代原先各插件对自研 asyncpg 池（v2.0.0 已删除）的内部耦合测试：
连接来源统一切换到 nonebot-plugin-orm 共享引擎后，所有适配行为（原生 SQL
$n 占位符、向量/数组绑定、事务边界、advisory lock 语义、归还语义）都在这
个公共连接层上断言一次，四个插件的仓库测试再直接复用该连接。

依赖已执行 ``alembic upgrade head`` 的迁移管理 schema（``KOMARI_TEST_POSTGRES_URL``
门控）。每个测试独立事件循环，测试前后 dispose 共享引擎，避免连接池跨循环
复用。
"""

from __future__ import annotations

import os
from contextlib import suppress
from urllib.parse import urlparse

import pytest

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")


def _configured_database_url() -> str:
    """返回 nonebot-plugin-orm 配置的数据库 URL（测试须与门控 URL 同库）。"""
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
    """剥离 ``+asyncpg`` scheme，得到 asyncpg 直连可解析的 URL。"""
    return POSTGRES_URL.replace("postgresql+asyncpg://", "postgresql://")


async def _reset_shared_orm_engine() -> None:
    """清空 nonebot-plugin-orm 共享引擎连接池（每个测试独立事件循环）。"""
    from nonebot import require

    require("nonebot_plugin_orm")
    import nonebot_plugin_orm as orm_module

    engines = getattr(orm_module, "_engines", None)
    if not engines:
        return
    for engine in list(engines.values()):
        with suppress(Exception):
            await engine.dispose()


async def _cleanup_temp_tables(*table_names: str) -> None:
    """删除测试创建的临时表（asyncpg 直连，与 ORM 引擎无关）。"""
    import asyncpg

    admin = await asyncpg.connect(_asyncpg_url())
    try:
        for table_name in table_names:
            await admin.execute(f"DROP TABLE IF EXISTS {table_name}")
    finally:
        await admin.close()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_shared_pool_acquire_runs_raw_sql_on_configured_database() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    from komari_bot.common.orm_connection import get_shared_orm_connection_pool

    pool = get_shared_orm_connection_pool()
    try:
        async with pool.acquire() as conn:
            import asyncpg

            assert isinstance(conn, asyncpg.Connection)
            await conn.execute("CREATE TEMP TABLE adapter_probe (id INT PRIMARY KEY)")
            await conn.execute("INSERT INTO adapter_probe VALUES ($1)", 1)
            row = await conn.fetchrow("SELECT id FROM adapter_probe WHERE id = $1", 1)
            assert row is not None
            assert row["id"] == 1
            total = await conn.fetchval("SELECT COUNT(*) FROM adapter_probe")
            assert total == 1
            rows = await conn.fetch("SELECT id FROM adapter_probe ORDER BY id")
            assert [item["id"] for item in rows] == [1]
    finally:
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_shared_pool_transaction_commit_and_rollback() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    from komari_bot.common.orm_connection import get_shared_orm_connection_pool

    pool = get_shared_orm_connection_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "CREATE TEMP TABLE adapter_tx (id INT PRIMARY KEY)"
            )
            async with conn.transaction():
                await conn.execute("INSERT INTO adapter_tx VALUES ($1)", 1)
            committed = await conn.fetchval("SELECT COUNT(*) FROM adapter_tx")
            assert committed == 1
            try:
                async with conn.transaction():
                    await conn.execute("INSERT INTO adapter_tx VALUES ($1)", 2)
                    raise RuntimeError("回滚")  # noqa: TRY301
            except RuntimeError:
                pass
            rolled_back = await conn.fetchval("SELECT COUNT(*) FROM adapter_tx")
            assert rolled_back == 1
    finally:
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_shared_pool_readonly_repeatable_read_transaction() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    from komari_bot.common.orm_connection import get_shared_orm_connection_pool

    pool = get_shared_orm_connection_pool()
    try:
        async with (
            pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
                snapshot = await conn.fetchval(
                    "SELECT COUNT(*) FROM komari_search_index_versions"
                )
                assert isinstance(snapshot, int) and snapshot >= 0
    finally:
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_shared_pool_binds_pgvector_arrays_and_jsonb() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    from komari_bot.common.orm_connection import get_shared_orm_connection_pool

    pool = get_shared_orm_connection_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TEMP TABLE adapter_vec (
                    id INT PRIMARY KEY,
                    embedding vector(2),
                    tags TEXT[],
                    payload JSONB
                )
                """
            )
            await conn.execute(
                """
                INSERT INTO adapter_vec VALUES ($1, $2::vector, $3, $4::jsonb)
                """,
                1,
                str([0.1, 0.2]),
                ["a", "b"],
                '{"k": 1}',
            )
            similarity = await conn.fetchval(
                """
                SELECT 1 - (embedding <=> $1::vector)
                FROM adapter_vec
                WHERE id = $2
                """,
                str([0.1, 0.2]),
                1,
            )
            assert similarity is not None
            assert round(float(similarity), 6) == 1.0
            tags = await conn.fetchval(
                "SELECT tags FROM adapter_vec WHERE id = ANY($1::int[])",
                [1],
            )
            assert tags is not None
            assert list(tags) == ["a", "b"]
    finally:
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_shared_pool_advisory_xact_lock_released_on_commit() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    from komari_bot.common.orm_connection import get_shared_orm_connection_pool

    pool = get_shared_orm_connection_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                locked = await conn.fetchval(
                    "SELECT pg_try_advisory_xact_lock($1)", 42
                )
                assert locked is True
            # xact 级锁在事务提交后自动释放：同一连接再次获取必须成功
            async with conn.transaction():
                relocked = await conn.fetchval(
                    "SELECT pg_try_advisory_xact_lock($1)", 42
                )
                assert relocked is True
    finally:
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_shared_pool_executemany_batches_statements() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    from komari_bot.common.orm_connection import get_shared_orm_connection_pool

    pool = get_shared_orm_connection_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "CREATE TEMP TABLE adapter_many (id INT PRIMARY KEY, name TEXT)"
            )
            await conn.executemany(
                "INSERT INTO adapter_many VALUES ($1, $2)",
                [(1, "a"), (2, "b"), (3, "c")],
            )
            total = await conn.fetchval("SELECT COUNT(*) FROM adapter_many")
            assert total == 3
    finally:
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_shared_pool_close_is_noop_and_pool_stays_usable() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    from komari_bot.common.orm_connection import get_shared_orm_connection_pool

    pool = get_shared_orm_connection_pool()
    try:
        await pool.close()
        async with pool.acquire() as conn:
            total = await conn.fetchval("SELECT 1")
            assert total == 1
    finally:
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
def test_shared_pool_is_process_singleton() -> None:
    from komari_bot.common.orm_connection import get_shared_orm_connection_pool

    first = get_shared_orm_connection_pool()
    second = get_shared_orm_connection_pool()
    assert first is second


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_shared_pool_probe_reports_database_availability() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    from komari_bot.common.orm_connection import get_shared_orm_connection_pool

    pool = get_shared_orm_connection_pool()
    try:
        assert await pool.probe() is True
    finally:
        await _reset_shared_orm_engine()
