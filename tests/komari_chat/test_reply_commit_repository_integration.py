"""可选的聊天副作用 outbox PostgreSQL 集成测试。

依赖已执行 alembic upgrade head 的迁移管理 schema；测试使用唯一 operation_id 并清理行。
"""

from __future__ import annotations

import os
from uuid import uuid4

import asyncpg
import pytest

from komari_bot.plugins.komari_chat.repositories.reply_commit_repository import (
    PendingReplyCommit,
    ReplyCommitRepository,
)

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")


def _asyncpg_url() -> str:
    """剥离 ``+asyncpg`` scheme，得到 asyncpg 直连可解析的 URL。"""
    return POSTGRES_URL.replace("postgresql+asyncpg://", "postgresql://")


def _payload(operation_id: str) -> PendingReplyCommit:
    return PendingReplyCommit(
        operation_id=operation_id,
        request_trace_id="chat-message-1",
        source_message_id="message-1",
        group_id="group-1",
        user_id="user-1",
        user_nickname="测试用户",
        bot_nickname="小鞠",
        reply_content="持久 outbox 回复",
        reply_timestamp=123.5,
        favorability_delta=1,
        favorability_reason="正常互动",
        interaction_history={
            "event": "用户发言",
            "result": "机器人回复",
            "emotion": "平静",
        },
        proactive_reservation_id="reservation-1",
        proactive_cooldown_seconds=300,
        global_interaction_enabled=True,
        global_interaction_trigger_size=20,
    )


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_reply_commit_outbox_prepare_claim_steps_and_tombstone() -> None:
    run_id = uuid4().hex
    operation_id = f"operation-1-{run_id}"
    cancelled_operation_id = f"operation-2-{run_id}"
    pool = await asyncpg.create_pool(_asyncpg_url(), min_size=1, max_size=2)
    try:
        repository = ReplyCommitRepository(pool)
        payload = _payload(operation_id)

        assert await repository.prepare(payload) is True
        assert await repository.prepare(payload) is False
        assert await repository.has_active_operation(payload.operation_id) is True
        assert await repository.mark_delivered(
            payload.operation_id,
            platform_message_id="platform-message-9",
        ) is True

        claimed = await repository.claim_operation(
            payload.operation_id,
            owner_token="worker-1",
            lease_seconds=60,
        )
        assert claimed is not None
        assert claimed["status"] == "PROCESSING"
        assert claimed["attempt_count"] == 1
        assert await repository.renew_lease(
            payload.operation_id,
            owner_token="worker-1",
            lease_seconds=60,
        )

        for step in (
            "proactive_confirmed",
            "favorability_applied",
            "ai_history_stored",
            "interaction_stored",
        ):
            assert await repository.mark_step(
                payload.operation_id,
                owner_token="worker-1",
                step=step,
            )
        assert await repository.complete(
            payload.operation_id,
            owner_token="worker-1",
        )

        async with pool.acquire() as connection:
            completed = await connection.fetchrow(
                """
                SELECT status, request_trace_id, platform_message_id,
                       reply_content, interaction_history,
                       proactive_reservation_id, completed_at
                FROM komari_chat_reply_commit_outbox
                WHERE operation_id = $1
                """,
                payload.operation_id,
            )
        assert completed is not None
        assert completed["status"] == "COMPLETED"
        assert completed["request_trace_id"] == "chat-message-1"
        assert completed["platform_message_id"] == "platform-message-9"
        assert completed["reply_content"] is None
        assert completed["interaction_history"] is None
        assert completed["proactive_reservation_id"] is None
        assert completed["completed_at"] is not None

        cancelled_payload = _payload(cancelled_operation_id)
        assert await repository.prepare(cancelled_payload) is True
        assert await repository.cancel_prepared(cancelled_payload.operation_id) is True
        assert (
            await repository.has_active_operation(cancelled_payload.operation_id)
            is False
        )
        assert await repository.prepare(cancelled_payload) is True
    finally:
        async with pool.acquire() as connection:
            await connection.execute(
                """
                DELETE FROM komari_chat_reply_commit_outbox
                WHERE operation_id = ANY($1::text[])
                """,
                [operation_id, cancelled_operation_id],
            )
        await pool.close()
