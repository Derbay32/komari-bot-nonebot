"""可选的维护公告幂等账本 PostgreSQL 集成测试。"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import asyncpg
import pytest

from komari_bot.plugins.komari_management import announcement_repository
from komari_bot.plugins.komari_management.announcement_repository import (
    AnnouncementDispatchRepository,
)

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_announcement_claim_is_single_owner_and_replayable() -> None:
    schema_name = f"announcement_dispatch_{uuid4().hex}"
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
        async with pool.acquire() as connection:
            await connection.execute(announcement_repository._SCHEMA_SQL)

        first = AnnouncementDispatchRepository()
        second = AnnouncementDispatchRepository()
        first._pool = pool
        second._pool = pool

        async def _claim(
            repository: AnnouncementDispatchRepository,
            owner_token: str,
        ) -> tuple[str, str]:
            claim = await repository.claim(
                request_id="integration-request",
                payload_hash="payload-hash",
                owner_token=owner_token,
                lease_seconds=60,
                cooldown_seconds=0,
            )
            return owner_token, claim.state

        claims = await asyncio.gather(
            _claim(first, "worker-1"),
            _claim(second, "worker-2"),
        )
        assert sorted(state for _, state in claims) == ["claimed", "in_progress"]
        winner = next(owner for owner, state in claims if state == "claimed")
        assert await first.complete(
            request_id="integration-request",
            owner_token=winner,
            response_payload={"total": 1, "results": []},
        )

        replay = await second.claim(
            request_id="integration-request",
            payload_hash="payload-hash",
            owner_token="worker-3",
            lease_seconds=60,
            cooldown_seconds=3600,
        )
        assert replay.state == "replay"
        assert replay.response_payload == {"total": 1, "results": []}

        conflict = await second.claim(
            request_id="integration-request",
            payload_hash="different-payload",
            owner_token="worker-3",
            lease_seconds=60,
            cooldown_seconds=0,
        )
        assert conflict.state == "payload_conflict"

        cooldown = await second.claim(
            request_id="new-request",
            payload_hash="new-payload",
            owner_token="worker-3",
            lease_seconds=60,
            cooldown_seconds=3600,
        )
        assert cooldown.state == "cooldown"
        assert cooldown.remaining_seconds is not None
        assert cooldown.remaining_seconds > 0

        unstarted = await second.claim(
            request_id="unstarted-request",
            payload_hash="unstarted-payload",
            owner_token="worker-4",
            lease_seconds=60,
            cooldown_seconds=0,
        )
        assert unstarted.state == "claimed"
        assert await second.cancel_unstarted(
            request_id="unstarted-request",
            owner_token="worker-4",
        )
        reclaimed = await first.claim(
            request_id="unstarted-request",
            payload_hash="unstarted-payload",
            owner_token="worker-5",
            lease_seconds=60,
            cooldown_seconds=0,
        )
        assert reclaimed.state == "claimed"
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE")
        await admin.close()
