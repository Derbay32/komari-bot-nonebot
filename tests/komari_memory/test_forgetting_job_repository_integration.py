"""可选的真实 PostgreSQL 每日忘却账本集成测试。"""

from __future__ import annotations

import asyncio
import os
from datetime import date
from uuid import uuid4

import asyncpg
import pytest

from komari_bot.plugins.komari_memory.repositories.forgetting_job_repository import (
    ForgettingJobLeaseLostError,
    ForgettingJobRepository,
)

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
def test_real_postgres_daily_job_owner_and_transactional_stage() -> None:
    async def _run() -> None:
        schema = f"komari_forgetting_test_{uuid4().hex}"
        admin = await asyncpg.connect(POSTGRES_URL)
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        try:
            await admin.execute(
                f"""
                CREATE TABLE "{schema}".komari_memory_jobs (
                    job_name TEXT NOT NULL,
                    run_date DATE NOT NULL,
                    owner_token TEXT NOT NULL,
                    lease_until TIMESTAMPTZ NOT NULL,
                    stage TEXT NOT NULL,
                    attempt INT NOT NULL DEFAULT 0,
                    last_error_code TEXT,
                    started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMPTZ,
                    PRIMARY KEY (job_name, run_date)
                );
                CREATE TABLE "{schema}".stage_effects (
                    counter INT NOT NULL
                );
                INSERT INTO "{schema}".stage_effects (counter) VALUES (0)
                """
            )
            settings = {"search_path": schema}
            first_pool = await asyncpg.create_pool(
                POSTGRES_URL,
                min_size=1,
                max_size=1,
                server_settings=settings,
            )
            second_pool = await asyncpg.create_pool(
                POSTGRES_URL,
                min_size=1,
                max_size=1,
                server_settings=settings,
            )
            if first_pool is None or second_pool is None:
                raise AssertionError
            first = ForgettingJobRepository(first_pool)
            second = ForgettingJobRepository(second_pool)
            run_date = date(2026, 7, 17)
            try:
                first_claim = await first.claim(
                    run_date=run_date,
                    owner_token="owner-1",
                    lease_seconds=60,
                )
                busy_claim = await second.claim(
                    run_date=run_date,
                    owner_token="owner-2",
                    lease_seconds=60,
                )
                async with first_pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE komari_memory_jobs
                        SET lease_until = NOW() - INTERVAL '1 second'
                        """
                    )
                takeover = await second.claim(
                    run_date=run_date,
                    owner_token="owner-2",
                    lease_seconds=60,
                )

                assert first_claim.status == "claimed"
                assert busy_claim.status == "busy"
                assert takeover.status == "claimed"
                with pytest.raises(ForgettingJobLeaseLostError):
                    await first.advance_stage(
                        run_date=run_date,
                        owner_token="owner-1",
                        lease_seconds=60,
                        expected_stage="claimed",
                        next_stage="conversation_decay_done",
                    )

                await second.run_transactional_stage(
                    run_date=run_date,
                    owner_token="owner-2",
                    lease_seconds=60,
                    expected_stage="claimed",
                    next_stage="conversation_decay_done",
                    actions=(("UPDATE stage_effects SET counter = counter + 1", ()),),
                )
                await second.advance_stage(
                    run_date=run_date,
                    owner_token="owner-2",
                    lease_seconds=60,
                    expected_stage="conversation_decay_done",
                    next_stage="completed",
                )
                completed = await first.claim(
                    run_date=run_date,
                    owner_token="owner-3",
                    lease_seconds=60,
                )
                async with first_pool.acquire() as conn:
                    counter = await conn.fetchval("SELECT counter FROM stage_effects")

                assert completed.status == "completed"
                assert counter == 1
            finally:
                await first_pool.close()
                await second_pool.close()
        finally:
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
            await admin.close()

    asyncio.run(_run())
