"""可选的真实 PostgreSQL 每日忘却账本集成测试。

任务表由 Alembic 迁移管理；测试使用远期唯一 run_date 并清理任务行，
仅保留 stage_effects 夹具表用于验证阶段事务性。仓库连接来源为插件公开的
``create_pool()``（nonebot-plugin-orm 共享引擎租约）。
"""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from datetime import date
from urllib.parse import urlparse

import asyncpg
import pytest

from komari_bot.plugins.komari_memory.repositories.forgetting_job_repository import (
    FORGETTING_JOB_NAME,
    ForgettingJobLeaseLostError,
    ForgettingJobRepository,
)

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")

_RUN_DATE = date(9999, 12, 31)


def _asyncpg_url() -> str:
    return POSTGRES_URL.replace("postgresql+asyncpg://", "postgresql://")


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
def test_real_postgres_daily_job_owner_and_transactional_stage() -> None:
    async def _run() -> None:
        if not _same_database(POSTGRES_URL, _configured_database_url()):
            pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
        await _reset_shared_orm_engine()
        from komari_bot.plugins.komari_memory.database.connection import create_pool

        admin = await asyncpg.connect(_asyncpg_url())
        await admin.execute(
            """
            CREATE TABLE IF NOT EXISTS stage_effects (
                counter INT NOT NULL
            )
            """
        )
        await admin.execute("DELETE FROM stage_effects")
        await admin.execute("INSERT INTO stage_effects (counter) VALUES (0)")
        first_pool = await create_pool()
        second_pool = await create_pool()
        try:
            async with admin.transaction():
                await admin.execute(
                    "DELETE FROM komari_memory_jobs WHERE job_name = $1 AND run_date = $2",
                    FORGETTING_JOB_NAME,
                    _RUN_DATE,
                )

            first = ForgettingJobRepository(first_pool)  # type: ignore[arg-type]
            second = ForgettingJobRepository(second_pool)  # type: ignore[arg-type]
            try:
                first_claim = await first.claim(
                    run_date=_RUN_DATE,
                    owner_token="owner-1",
                    lease_seconds=60,
                )
                busy_claim = await second.claim(
                    run_date=_RUN_DATE,
                    owner_token="owner-2",
                    lease_seconds=60,
                )
                async with admin.transaction():
                    await admin.execute(
                        """
                        UPDATE komari_memory_jobs
                        SET lease_until = NOW() - INTERVAL '1 second'
                        WHERE job_name = $1 AND run_date = $2
                        """,
                        FORGETTING_JOB_NAME,
                        _RUN_DATE,
                    )
                takeover = await second.claim(
                    run_date=_RUN_DATE,
                    owner_token="owner-2",
                    lease_seconds=60,
                )

                assert first_claim.status == "claimed"
                assert busy_claim.status == "busy"
                assert takeover.status == "claimed"
                with pytest.raises(ForgettingJobLeaseLostError):
                    await first.advance_stage(
                        run_date=_RUN_DATE,
                        owner_token="owner-1",
                        lease_seconds=60,
                        expected_stage="claimed",
                        next_stage="conversation_decay_done",
                    )

                await second.run_transactional_stage(
                    run_date=_RUN_DATE,
                    owner_token="owner-2",
                    lease_seconds=60,
                    expected_stage="claimed",
                    next_stage="conversation_decay_done",
                    actions=(("UPDATE stage_effects SET counter = counter + 1", ()),),
                )
                await second.advance_stage(
                    run_date=_RUN_DATE,
                    owner_token="owner-2",
                    lease_seconds=60,
                    expected_stage="conversation_decay_done",
                    next_stage="completed",
                )
                completed = await first.claim(
                    run_date=_RUN_DATE,
                    owner_token="owner-3",
                    lease_seconds=60,
                )
                counter = await admin.fetchval("SELECT counter FROM stage_effects")

                assert completed.status == "completed"
                assert counter == 1
            finally:
                async with admin.transaction():
                    await admin.execute(
                        """
                        DELETE FROM komari_memory_jobs
                        WHERE job_name = $1 AND run_date = $2
                        """,
                        FORGETTING_JOB_NAME,
                        _RUN_DATE,
                    )
        finally:
            await first_pool.close()  # type: ignore[attr-defined]
            await second_pool.close()  # type: ignore[attr-defined]
            await admin.execute("DROP TABLE IF EXISTS stage_effects")
            await admin.close()
            await _reset_shared_orm_engine()

    asyncio.run(_run())
