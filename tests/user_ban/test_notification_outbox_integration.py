"""可选的 User Ban 自然解封 outbox PostgreSQL 集成测试。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from komari_bot.plugins.user_ban.models import ExpiredBanNotification
from komari_bot.plugins.user_ban.repository import UserBanRepository

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_expiry_delete_and_outbox_claim_are_transactional_and_single_owner() -> (
    None
):
    schema_name = f"user_ban_outbox_{uuid4().hex}"
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
        init_sql = (
            Path(__file__).parents[2]
            / "komari_bot"
            / "plugins"
            / "user_ban"
            / "init_db.sql"
        ).read_text(encoding="utf-8")
        async with pool.acquire() as connection:
            await connection.execute(init_sql)
            await connection.execute(
                """
                INSERT INTO komari_user_bans (
                    user_id,
                    ban_scope,
                    operator_id,
                    reason,
                    expires_at
                )
                VALUES ('10086', 'chat', 'integration', '到期集成测试',
                        CURRENT_TIMESTAMP - INTERVAL '1 second')
                """
            )

        first = UserBanRepository()
        second = UserBanRepository()
        first._pool = pool
        second._pool = pool
        expired = await first.delete_expired()
        assert len(expired) == 1

        async def _claim(
            repository: UserBanRepository,
            owner_token: str,
        ) -> tuple[str, ExpiredBanNotification | None]:
            notification = await repository.claim_expired_notification(
                owner_token=owner_token,
                lease_seconds=60,
            )
            return owner_token, notification

        claimed = await asyncio.gather(
            _claim(first, "worker-1"),
            _claim(second, "worker-2"),
        )
        winners = [(owner, item) for owner, item in claimed if item is not None]
        assert len(winners) == 1
        owner_token, notification = winners[0]
        assert isinstance(notification, ExpiredBanNotification)
        assert notification.user_id == "10086"
        assert notification.records[0].reason == "到期集成测试"
        assert await first.acknowledge_expired_notification(
            notification_id=notification.notification_id,
            owner_token=owner_token,
        )

        async with pool.acquire() as connection:
            ban_count = await connection.fetchval(
                "SELECT COUNT(*) FROM komari_user_bans"
            )
            outbox = await connection.fetchrow(
                """
                SELECT status, records, owner_token, sent_at
                FROM komari_user_ban_notification_outbox
                """
            )
        assert ban_count == 0
        assert outbox is not None
        assert outbox["status"] == "sent"
        assert outbox["records"] is None
        assert outbox["owner_token"] is None
        assert outbox["sent_at"] is not None
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE")
        await admin.close()
