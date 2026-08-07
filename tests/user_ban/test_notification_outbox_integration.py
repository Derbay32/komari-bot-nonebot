"""可选的 User Ban 自然解封 outbox PostgreSQL 集成测试。

依赖已执行 alembic upgrade head 的迁移管理 schema；测试使用唯一用户行并在结束时清理。
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import asyncpg
import pytest

from komari_bot.plugins.user_ban.models import ExpiredBanNotification
from komari_bot.plugins.user_ban.repository import UserBanRepository

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")
# asyncpg 只接受标准 postgresql:// scheme，剥掉 SQLAlchemy 方言后缀
ASYNC_PG_URL = POSTGRES_URL.replace("postgresql+asyncpg://", "postgresql://", 1)


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_expiry_delete_and_outbox_claim_are_transactional_and_single_owner() -> (
    None
):
    # nonebot_plugin_orm 首次 import 必须在插件加载上下文内完成
    # （模块导入即解析 localstore 数据目录），先经 require 加载；
    # 共享引擎连接池跨事件循环不可复用，每个测试前清空一次。
    from nonebot import require

    require("nonebot_plugin_orm")
    from contextlib import suppress

    import nonebot_plugin_orm as orm_module

    for engine in list(getattr(orm_module, "_engines", {}).values()):
        with suppress(Exception):
            await engine.dispose()

    user_id = f"ban-{uuid4().hex}"
    pool = await asyncpg.create_pool(ASYNC_PG_URL, min_size=1, max_size=4)
    outbox_ids: list[str] = []
    try:
        async with pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO komari_user_bans (
                    user_id,
                    ban_scope,
                    operator_id,
                    reason,
                    expires_at
                )
                VALUES ($1, 'chat', 'integration', '到期集成测试',
                        CURRENT_TIMESTAMP - INTERVAL '1 second')
                """,
                user_id,
            )

        # ORM 化后仓储直接使用 nonebot-plugin-orm 共享引擎，
        # 不再存在可注入的私有连接池
        first = UserBanRepository()
        second = UserBanRepository()
        expired = await first.delete_expired()
        assert any(entry.user_id == user_id for entry in expired)

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
            _claim(first, f"worker-1-{uuid4().hex}"),
            _claim(second, f"worker-2-{uuid4().hex}"),
        )
        candidates = [
            (owner, item) for owner, item in claimed if item is not None
        ]
        assert candidates
        async with pool.acquire() as connection:
            outbox_rows = await connection.fetch(
                """
                SELECT notification_id, status
                FROM komari_user_ban_notification_outbox
                WHERE owner_token = ANY($1::text[]) AND status <> 'sent'
                """,
                [owner for owner, _ in claimed],
            )
            outbox_ids = [row["notification_id"] for row in outbox_rows]
            ban_count = await connection.fetchval(
                "SELECT COUNT(*) FROM komari_user_bans WHERE user_id = $1",
                user_id,
            )
        assert ban_count == 0
        assert len(candidates) == 1 or all(
            notification.user_id == user_id
            for _, notification in candidates
        )
        winner_owner, notification = candidates[0]
        assert isinstance(notification, ExpiredBanNotification)
        assert notification.user_id == user_id
        assert notification.records[0].reason == "到期集成测试"
        assert await first.acknowledge_expired_notification(
            notification_id=notification.notification_id,
            owner_token=winner_owner,
        )

        async with pool.acquire() as connection:
            outbox = await connection.fetchrow(
                """
                SELECT status, records, owner_token, sent_at
                FROM komari_user_ban_notification_outbox
                WHERE notification_id = $1
                """,
                notification.notification_id,
            )
        assert outbox is not None
        assert outbox["status"] == "sent"
        assert outbox["records"] is None
        assert outbox["owner_token"] is None
        assert outbox["sent_at"] is not None
    finally:
        async with pool.acquire() as connection:
            await connection.execute(
                "DELETE FROM komari_user_bans WHERE user_id = $1",
                user_id,
            )
            if outbox_ids:
                await connection.execute(
                    """
                    DELETE FROM komari_user_ban_notification_outbox
                    WHERE notification_id = ANY($1::text[])
                    """,
                    outbox_ids,
                )
        await pool.close()
