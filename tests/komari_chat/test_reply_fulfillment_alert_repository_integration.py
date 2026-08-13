"""回复履约告警持久化去重的真实 PostgreSQL 验收测试。"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import pytest

from komari_bot.plugins.komari_chat.repositories.reply_fulfillment_repository import (
    AssistantReplyHistoryPayload,
    FavorabilityAdjustmentPayload,
    InteractionHistoryPayload,
    ProactiveReplyConfirmationPayload,
    ReplyCommitmentInput,
    ReplyFulfillmentDraft,
    ReplyFulfillmentRepository,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")
SQLALCHEMY_URL = os.getenv("SQLALCHEMY_DATABASE_URL", "")


def _database_identity(url: str) -> tuple[str, int | None, str]:
    parsed = urlsplit(url.replace("postgresql+asyncpg://", "postgresql://"))
    return parsed.hostname or "", parsed.port, parsed.path


DATABASE_CONFIGURED = bool(
    POSTGRES_URL
    and SQLALCHEMY_URL
    and _database_identity(POSTGRES_URL) == _database_identity(SQLALCHEMY_URL)
)

pytestmark = [
    pytest.mark.skipif(
        not DATABASE_CONFIGURED,
        reason="KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 未指向同一真实库",
    ),
    pytest.mark.asyncio,
]


def _asyncpg_url() -> str:
    return POSTGRES_URL.replace("postgresql+asyncpg://", "postgresql://")


def _draft(fulfillment_id: str) -> ReplyFulfillmentDraft:
    return ReplyFulfillmentDraft(
        fulfillment_id=fulfillment_id,
        payload_hash="a" * 64,
        request_trace_id=f"trace-{fulfillment_id}",
        trigger_message_id=f"message-{fulfillment_id}",
        trigger_user_id="user-1",
        group_id="group-1",
        bot_self_id="bot-1",
        adapter_name="onebot.v11",
        reply_target_message_id="message-target-1",
        reply_content="持久化的角色回复",
        commitments=(
            ReplyCommitmentInput(
                commitment_type="proactive_reply_confirmation",
                payload=ProactiveReplyConfirmationPayload(
                    group_id="group-1",
                    reservation_id="reservation-1",
                    cooldown_seconds=300,
                ),
            ),
            ReplyCommitmentInput(
                commitment_type="favorability_adjustment",
                payload=FavorabilityAdjustmentPayload(
                    user_id="user-1",
                    delta=1,
                    reason="正常互动",
                ),
            ),
            ReplyCommitmentInput(
                commitment_type="assistant_reply_history",
                payload=AssistantReplyHistoryPayload(
                    group_id="group-1",
                    bot_nickname="小鞠",
                    reply_content="持久化的角色回复",
                    reply_timestamp=123.5,
                ),
            ),
            ReplyCommitmentInput(
                commitment_type="interaction_history",
                payload=InteractionHistoryPayload(
                    user_id="user-1",
                    display_name="测试用户",
                    trigger_size=20,
                    reply_timestamp=123.5,
                    trigger_message_id=f"message-{fulfillment_id}",
                    record={
                        "event": "用户发言",
                        "result": "机器人回复",
                        "emotion": "平静",
                    },
                ),
            ),
        ),
    )


@asynccontextmanager
async def _repository_context(
    fulfillment_ids: list[str],
) -> AsyncIterator[tuple[ReplyFulfillmentRepository, asyncpg.Pool]]:
    pool = await asyncpg.create_pool(_asyncpg_url(), min_size=1, max_size=4)
    try:
        yield ReplyFulfillmentRepository(pool), pool
    finally:
        async with pool.acquire() as connection:
            await connection.execute(
                """
                DELETE FROM komari_chat_reply_fulfillments
                WHERE fulfillment_id = ANY($1::text[])
                """,
                fulfillment_ids,
            )
        await pool.close()


async def _prepare_delivered(
    repository: ReplyFulfillmentRepository,
    fulfillment_id: str,
) -> None:
    assert await repository.prepare(_draft(fulfillment_id))
    assert await repository.mark_send_started(fulfillment_id)
    assert await repository.mark_delivered(fulfillment_id)


async def test_pending_confirmation_alert_claim_is_persistent_and_concurrent_safe() -> (
    None
):
    """待确认转换由数据库原子领取，并发与仓库重建都不会重复命中。"""
    fulfillment_id = f"alert-pending-{uuid4().hex}"
    async with _repository_context([fulfillment_id]) as (repository, pool):
        assert await repository.prepare(_draft(fulfillment_id))
        assert await repository.mark_send_started(fulfillment_id)

        first, second = await asyncio.gather(
            repository.claim_pending_confirmation_alerts(limit=10),
            repository.claim_pending_confirmation_alerts(limit=10),
        )

        assert sorted((len(first), len(second))) == [0, 1]
        claimed = (first or second)[0]
        assert claimed == {
            "fulfillment_id": fulfillment_id,
            "status": "pending_confirmation",
            "commitment_type": None,
            "error_code": None,
        }
        restarted = ReplyFulfillmentRepository(pool)
        assert await restarted.claim_pending_confirmation_alerts(limit=10) == []
        async with pool.acquire() as connection:
            alerted_at = await connection.fetchval(
                """
                SELECT pending_confirmation_alerted_at
                FROM komari_chat_reply_fulfillments
                WHERE fulfillment_id = $1
                """,
                fulfillment_id,
            )
        assert alerted_at is not None


async def test_disposition_alert_resets_only_on_authorized_resume() -> None:
    """续跑开启同一承诺的新告警代际，不修改冻结载荷或兄弟承诺。"""
    fulfillment_id = f"alert-disposition-{uuid4().hex}"
    commitment_type = "favorability_adjustment"
    async with _repository_context([fulfillment_id]) as (repository, pool):
        await _prepare_delivered(repository, fulfillment_id)
        assert await repository.claim_lease(
            fulfillment_id,
            owner_token="alert-worker-1",
            lease_seconds=60,
        )
        assert (
            await repository.mark_commitment_failed(
                fulfillment_id,
                commitment_type=commitment_type,
                owner_token="alert-worker-1",
                error_code="invalid_payload",
                max_attempts=1,
                retry_base_seconds=1,
            )
            == "FAILED"
        )
        assert await repository.release_lease(
            fulfillment_id,
            owner_token="alert-worker-1",
        )

        first = await repository.claim_commitment_disposition_alerts(limit=10)
        assert first == [
            {
                "fulfillment_id": fulfillment_id,
                "status": "needs_disposition",
                "commitment_type": commitment_type,
                "error_code": "invalid_payload",
            }
        ]
        assert await repository.claim_commitment_disposition_alerts(limit=10) == []

        async with pool.acquire() as connection:
            payload_before = await connection.fetchval(
                """
                SELECT payload
                FROM komari_chat_reply_fulfillment_commitments
                WHERE fulfillment_id = $1 AND commitment_type = $2
                """,
                fulfillment_id,
                commitment_type,
            )
            sibling_markers_before = await connection.fetch(
                """
                SELECT commitment_type, disposition_alerted_at
                FROM komari_chat_reply_fulfillment_commitments
                WHERE fulfillment_id = $1 AND commitment_type <> $2
                ORDER BY commitment_type
                """,
                fulfillment_id,
                commitment_type,
            )

        assert (
            await repository.resume_failed_commitment(
                fulfillment_id,
                commitment_type=commitment_type,
            )
            == "updated"
        )
        assert await repository.claim_lease(
            fulfillment_id,
            owner_token="alert-worker-2",
            lease_seconds=60,
        )
        assert (
            await repository.mark_commitment_failed(
                fulfillment_id,
                commitment_type=commitment_type,
                owner_token="alert-worker-2",
                error_code="invalid_payload",
                max_attempts=1,
                retry_base_seconds=1,
            )
            == "FAILED"
        )
        assert await repository.release_lease(
            fulfillment_id,
            owner_token="alert-worker-2",
        )

        second = await repository.claim_commitment_disposition_alerts(limit=10)
        assert second == first
        async with pool.acquire() as connection:
            payload_after = await connection.fetchval(
                """
                SELECT payload
                FROM komari_chat_reply_fulfillment_commitments
                WHERE fulfillment_id = $1 AND commitment_type = $2
                """,
                fulfillment_id,
                commitment_type,
            )
            sibling_markers_after = await connection.fetch(
                """
                SELECT commitment_type, disposition_alerted_at
                FROM komari_chat_reply_fulfillment_commitments
                WHERE fulfillment_id = $1 AND commitment_type <> $2
                ORDER BY commitment_type
                """,
                fulfillment_id,
                commitment_type,
            )
        assert payload_after == payload_before
        assert sibling_markers_after == sibling_markers_before


async def test_retry_wait_never_becomes_disposition_alert_candidate() -> None:
    """瞬态失败尚在自动重试预算内时不制造待处置告警。"""
    fulfillment_id = f"alert-retry-{uuid4().hex}"
    async with _repository_context([fulfillment_id]) as (repository, _pool):
        await _prepare_delivered(repository, fulfillment_id)
        assert await repository.claim_lease(
            fulfillment_id,
            owner_token="retry-worker",
            lease_seconds=60,
        )
        assert (
            await repository.mark_commitment_failed(
                fulfillment_id,
                commitment_type="favorability_adjustment",
                owner_token="retry-worker",
                error_code="transient_timeout",
                max_attempts=3,
                retry_base_seconds=1,
            )
            == "RETRY_WAIT"
        )
        assert await repository.release_lease(
            fulfillment_id,
            owner_token="retry-worker",
        )

        assert await repository.claim_commitment_disposition_alerts(limit=10) == []
