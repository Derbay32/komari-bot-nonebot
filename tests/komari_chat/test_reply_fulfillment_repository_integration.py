"""回复履约父子存储 adapter 的真实 PostgreSQL 验收测试。"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import replace
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
    ReplyFulfillmentConflictError,
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
    *,
    max_size: int = 4,
) -> AsyncIterator[tuple[ReplyFulfillmentRepository, asyncpg.Pool]]:
    pool = await asyncpg.create_pool(_asyncpg_url(), min_size=1, max_size=max_size)
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
    assert await repository.prepare(_draft(fulfillment_id)) is True
    assert await repository.mark_send_started(fulfillment_id) is True
    assert await repository.mark_delivered(fulfillment_id) is True


async def test_concurrent_prepare_creates_one_parent_and_fixed_children() -> None:
    """并发准备只产生一个父记录和一组固定、无重复的承诺子项。"""
    fulfillment_id = f"concurrent-prepare-{uuid4().hex}"
    async with _repository_context([fulfillment_id]) as (repository, pool):
        results = await asyncio.gather(
            repository.prepare(_draft(fulfillment_id)),
            repository.prepare(_draft(fulfillment_id)),
        )

        assert sorted(results) == [False, True]
        async with pool.acquire() as connection:
            parent_count = await connection.fetchval(
                """
                SELECT COUNT(*) FROM komari_chat_reply_fulfillments
                WHERE fulfillment_id = $1
                """,
                fulfillment_id,
            )
            children = await connection.fetch(
                """
                SELECT commitment_type, state, attempt_count,
                       jsonb_typeof(payload) AS payload_type
                FROM komari_chat_reply_fulfillment_commitments
                WHERE fulfillment_id = $1
                ORDER BY commitment_type
                """,
                fulfillment_id,
            )

        assert parent_count == 1
        assert {row["commitment_type"] for row in children} == {
            "proactive_reply_confirmation",
            "favorability_adjustment",
            "assistant_reply_history",
            "interaction_history",
        }
        assert all(row["state"] == "PENDING" for row in children)
        assert all(row["attempt_count"] == 0 for row in children)
        assert all(row["payload_type"] == "object" for row in children)


async def test_prepare_reuses_same_hash_and_rejects_payload_conflict() -> None:
    """同一身份只接受首次冻结的责任，重复准备不会覆盖父子载荷。"""
    fulfillment_id = f"prepare-conflict-{uuid4().hex}"
    async with _repository_context([fulfillment_id]) as (repository, pool):
        draft = _draft(fulfillment_id)
        assert await repository.prepare(draft) is True
        assert await repository.prepare(draft) is False

        with pytest.raises(ReplyFulfillmentConflictError, match="履约冲突"):
            await repository.prepare(
                replace(draft, payload_hash="b" * 64, reply_content="冲突回复")
            )

        async with pool.acquire() as connection:
            stored = await connection.fetchrow(
                """
                SELECT payload_hash, reply_content
                FROM komari_chat_reply_fulfillments
                WHERE fulfillment_id = $1
                """,
                fulfillment_id,
            )
        assert stored is not None
        assert stored["payload_hash"] == "a" * 64
        assert stored["reply_content"] == "持久化的角色回复"


async def test_delivery_fact_only_moves_forward() -> None:
    """送达事实只能从未发送向待确认，再进入一个互斥终态。"""
    delivered_id = f"delivery-forward-{uuid4().hex}"
    not_delivered_id = f"delivery-rejected-{uuid4().hex}"
    async with _repository_context([delivered_id, not_delivered_id]) as (
        repository,
        pool,
    ):
        assert await repository.prepare(_draft(delivered_id)) is True
        assert await repository.mark_delivered(delivered_id) is False
        assert await repository.mark_send_started(delivered_id) is True
        assert await repository.mark_delivered(
            delivered_id,
            platform_message_id="platform-1",
        ) is True
        assert await repository.mark_not_delivered(delivered_id) is False

        assert await repository.prepare(_draft(not_delivered_id)) is True
        assert await repository.mark_send_started(not_delivered_id) is True
        assert await repository.mark_not_delivered(not_delivered_id) is True
        assert await repository.mark_delivered(not_delivered_id) is False

        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT fulfillment_id, delivery_state, platform_message_id,
                       send_started_at, delivered_at, not_delivered_at
                FROM komari_chat_reply_fulfillments
                WHERE fulfillment_id = ANY($1::text[])
                """,
                [delivered_id, not_delivered_id],
            )
        by_id = {row["fulfillment_id"]: row for row in rows}
        assert by_id[delivered_id]["delivery_state"] == "DELIVERED"
        assert by_id[delivered_id]["platform_message_id"] == "platform-1"
        assert by_id[delivered_id]["send_started_at"] is not None
        assert by_id[delivered_id]["delivered_at"] is not None
        assert by_id[delivered_id]["not_delivered_at"] is None
        assert by_id[not_delivered_id]["delivery_state"] == "NOT_DELIVERED"
        assert by_id[not_delivered_id]["not_delivered_at"] is not None


async def test_delivery_confirmation_is_idempotent_and_conflicts_are_rejected() -> None:
    """同平台消息 ID 可重复确认，冲突 ID 不得覆盖既有送达事实。"""
    fulfillment_id = f"delivery-idempotent-{uuid4().hex}"
    async with _repository_context([fulfillment_id]) as (repository, pool):
        assert await repository.prepare(_draft(fulfillment_id)) is True
        assert await repository.mark_send_started(fulfillment_id) is True
        assert await repository.mark_delivered(
            fulfillment_id,
            platform_message_id="platform-1",
        ) is True
        assert await repository.mark_delivered(
            fulfillment_id,
            platform_message_id="platform-1",
        ) is True
        with pytest.raises(ValueError, match="平台消息 ID 冲突"):
            await repository.mark_delivered(
                fulfillment_id,
                platform_message_id="platform-2",
            )

        async with pool.acquire() as connection:
            stored = await connection.fetchrow(
                """
                SELECT delivery_state, platform_message_id
                FROM komari_chat_reply_fulfillments
                WHERE fulfillment_id = $1
                """,
                fulfillment_id,
            )
        assert stored["delivery_state"] == "DELIVERED"
        assert stored["platform_message_id"] == "platform-1"


async def test_not_started_recovery_respects_identity_and_freshness_boundary() -> None:
    """父表 adapter 同步提供精确 Bot 领取和准备时间边界语义。"""
    fresh_id = f"parent-fresh-{uuid4().hex}"
    stale_id = f"parent-stale-{uuid4().hex}"
    mismatch_id = f"parent-mismatch-{uuid4().hex}"
    fulfillment_ids = [fresh_id, stale_id, mismatch_id]
    async with _repository_context(fulfillment_ids) as (repository, pool):
        for fulfillment_id in fulfillment_ids:
            assert await repository.prepare(_draft(fulfillment_id)) is True

        async with pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE komari_chat_reply_fulfillments
                SET prepared_at = NOW() - INTERVAL '119 seconds'
                WHERE fulfillment_id = $1
                """,
                fresh_id,
            )
            await connection.execute(
                """
                UPDATE komari_chat_reply_fulfillments
                SET prepared_at = NOW() - INTERVAL '120 seconds'
                WHERE fulfillment_id = $1
                """,
                stale_id,
            )
            await connection.execute(
                """
                UPDATE komari_chat_reply_fulfillments
                SET adapter_name = 'other-adapter'
                WHERE fulfillment_id = $1
                """,
                mismatch_id,
            )

        claimed = await repository.claim_fresh_not_started(
            bot_self_id="bot-1",
            adapter_name="onebot.v11",
            freshness_seconds=120,
            limit=20,
        )
        assert [row["fulfillment_id"] for row in claimed] == [fresh_id]
        assert claimed[0]["delivery_state"] == "PENDING_CONFIRMATION"

        expired = await repository.expire_stale_not_started(
            freshness_seconds=120,
            limit=20,
        )
        assert [row["fulfillment_id"] for row in expired] == [stale_id]
        assert expired[0]["send_started_at"] is None
        assert expired[0]["not_delivered_at"] is not None

        async with pool.acquire() as connection:
            mismatch = await connection.fetchrow(
                """
                SELECT delivery_state, send_started_at
                FROM komari_chat_reply_fulfillments
                WHERE fulfillment_id = $1
                """,
                mismatch_id,
            )
        assert mismatch["delivery_state"] == "NOT_STARTED"
        assert mismatch["send_started_at"] is None


async def test_claim_pending_is_disjoint_and_skips_locked_parent() -> None:
    """并发 worker 领取集合互斥，且 SKIP LOCKED 不被其他事务阻塞。"""
    fulfillment_ids = [f"claim-{index}-{uuid4().hex}" for index in range(5)]
    async with _repository_context(fulfillment_ids) as (repository, pool):
        for fulfillment_id in fulfillment_ids:
            await _prepare_delivered(repository, fulfillment_id)

        first, second = await asyncio.gather(
            repository.claim_pending(
                owner_token="worker-a",
                limit=2,
                lease_seconds=60,
            ),
            repository.claim_pending(
                owner_token="worker-b",
                limit=2,
                lease_seconds=60,
            ),
        )
        first_ids = {row["fulfillment_id"] for row in first}
        second_ids = {row["fulfillment_id"] for row in second}
        assert len(first_ids) == 2
        assert len(second_ids) == 2
        assert first_ids.isdisjoint(second_ids)

        remaining_id = next(iter(set(fulfillment_ids) - first_ids - second_ids))
        locker = await pool.acquire()
        transaction = locker.transaction()
        try:
            await transaction.start()
            await locker.execute(
                """
                SELECT fulfillment_id
                FROM komari_chat_reply_fulfillments
                WHERE fulfillment_id = $1
                FOR UPDATE
                """,
                remaining_id,
            )
            skipped = await asyncio.wait_for(
                repository.claim_pending(
                    owner_token="worker-c",
                    limit=10,
                    lease_seconds=60,
                ),
                timeout=1,
            )
            assert remaining_id not in {
                row["fulfillment_id"] for row in skipped
            }
        finally:
            await transaction.rollback()
            await pool.release(locker)

        claimed_after_unlock = await repository.claim_pending(
            owner_token="worker-c",
            limit=10,
            lease_seconds=60,
        )
        assert {row["fulfillment_id"] for row in claimed_after_unlock} == {
            remaining_id
        }


async def test_expired_lease_is_reclaimed_and_old_owner_loses_cas() -> None:
    """过期父租约可回收，旧 owner 不能再续租、写子项或完成父记录。"""
    fulfillment_id = f"lease-reclaim-{uuid4().hex}"
    async with _repository_context([fulfillment_id]) as (repository, pool):
        await _prepare_delivered(repository, fulfillment_id)
        assert await repository.claim_operation(
            fulfillment_id,
            owner_token="old-owner",
            lease_seconds=60,
        ) is not None
        assert await repository.renew_lease(
            fulfillment_id,
            owner_token="old-owner",
            lease_seconds=60,
        ) is True

        async with pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE komari_chat_reply_fulfillments
                SET lease_expires_at = NOW() - INTERVAL '1 second'
                WHERE fulfillment_id = $1
                """,
                fulfillment_id,
            )

        assert await repository.renew_lease(
            fulfillment_id,
            owner_token="old-owner",
            lease_seconds=60,
        ) is False
        reclaimed = await repository.claim_operation(
            fulfillment_id,
            owner_token="new-owner",
            lease_seconds=60,
        )
        assert reclaimed is not None
        assert reclaimed["lease_owner"] == "new-owner"

        assert await repository.mark_commitment_completed(
            fulfillment_id,
            commitment_type="favorability_adjustment",
            owner_token="old-owner",
        ) is False
        assert await repository.mark_commitment_failed(
            fulfillment_id,
            commitment_type="favorability_adjustment",
            owner_token="old-owner",
            error_code="stale_owner",
            max_attempts=3,
            retry_base_seconds=1,
        ) is None
        assert await repository.complete_fulfillment(
            fulfillment_id,
            owner_token="old-owner",
        ) is False


async def test_commitments_retry_independently_and_gate_parent_completion() -> None:
    """一个承诺失败不阻塞其他项；父履约只在全部适用项完成后完成。"""
    fulfillment_id = f"child-retry-{uuid4().hex}"
    async with _repository_context([fulfillment_id]) as (repository, pool):
        await _prepare_delivered(repository, fulfillment_id)
        assert await repository.claim_operation(
            fulfillment_id,
            owner_token="worker-1",
            lease_seconds=60,
        ) is not None

        failure_state = await repository.mark_commitment_failed(
            fulfillment_id,
            commitment_type="favorability_adjustment",
            owner_token="worker-1",
            error_code="temporary_database_error",
            max_attempts=3,
            retry_base_seconds=10,
        )
        assert failure_state == "RETRY_WAIT"

        for commitment_type in (
            "proactive_reply_confirmation",
            "assistant_reply_history",
            "interaction_history",
        ):
            assert await repository.mark_commitment_completed(
                fulfillment_id,
                commitment_type=commitment_type,
                owner_token="worker-1",
            ) is True

        assert await repository.complete_fulfillment(
            fulfillment_id,
            owner_token="worker-1",
        ) is False
        assert await repository.release_lease(
            fulfillment_id,
            owner_token="worker-1",
        ) is True
        assert await repository.claim_pending(
            owner_token="worker-2",
            limit=10,
            lease_seconds=60,
        ) == []

        async with pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE komari_chat_reply_fulfillment_commitments
                SET next_retry_at = NOW() - INTERVAL '1 second'
                WHERE fulfillment_id = $1
                  AND commitment_type = 'favorability_adjustment'
                """,
                fulfillment_id,
            )

        reclaimed = await repository.claim_pending(
            owner_token="worker-2",
            limit=10,
            lease_seconds=60,
        )
        assert {row["fulfillment_id"] for row in reclaimed} == {fulfillment_id}
        assert await repository.mark_commitment_completed(
            fulfillment_id,
            commitment_type="favorability_adjustment",
            owner_token="worker-2",
        ) is True
        assert await repository.complete_fulfillment(
            fulfillment_id,
            owner_token="worker-2",
        ) is True

        async with pool.acquire() as connection:
            parent = await connection.fetchrow(
                """
                SELECT completed_at, lease_owner, lease_expires_at
                FROM komari_chat_reply_fulfillments
                WHERE fulfillment_id = $1
                """,
                fulfillment_id,
            )
            children = await connection.fetch(
                """
                SELECT commitment_type, state, attempt_count, last_error_code
                FROM komari_chat_reply_fulfillment_commitments
                WHERE fulfillment_id = $1
                ORDER BY commitment_type
                """,
                fulfillment_id,
            )
        assert parent["completed_at"] is not None
        assert parent["lease_owner"] is None
        assert parent["lease_expires_at"] is None
        assert all(row["state"] == "COMPLETED" for row in children)
        attempts = {row["commitment_type"]: row["attempt_count"] for row in children}
        assert attempts["favorability_adjustment"] == 2
        assert all(
            count == 1
            for commitment_type, count in attempts.items()
            if commitment_type != "favorability_adjustment"
        )


async def test_exhausted_child_blocks_completion_without_poisoning_siblings() -> None:
    """单项耗尽只令该子项待处置，已完成兄弟项保持完成且父记录不伪完成。"""
    fulfillment_id = f"child-failed-{uuid4().hex}"
    async with _repository_context([fulfillment_id]) as (repository, pool):
        await _prepare_delivered(repository, fulfillment_id)
        assert await repository.claim_operation(
            fulfillment_id,
            owner_token="worker-1",
            lease_seconds=60,
        ) is not None

        for commitment_type in (
            "proactive_reply_confirmation",
            "assistant_reply_history",
            "interaction_history",
        ):
            assert await repository.mark_commitment_completed(
                fulfillment_id,
                commitment_type=commitment_type,
                owner_token="worker-1",
            ) is True
        assert await repository.mark_commitment_failed(
            fulfillment_id,
            commitment_type="favorability_adjustment",
            owner_token="worker-1",
            error_code="invalid_payload",
            max_attempts=1,
            retry_base_seconds=1,
        ) == "FAILED"
        assert await repository.complete_fulfillment(
            fulfillment_id,
            owner_token="worker-1",
        ) is False

        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT commitment_type, state, next_retry_at
                FROM komari_chat_reply_fulfillment_commitments
                WHERE fulfillment_id = $1
                """,
                fulfillment_id,
            )
            completed_at = await connection.fetchval(
                """
                SELECT completed_at FROM komari_chat_reply_fulfillments
                WHERE fulfillment_id = $1
                """,
                fulfillment_id,
            )
        by_type = {row["commitment_type"]: row for row in rows}
        assert by_type["favorability_adjustment"]["state"] == "FAILED"
        assert by_type["favorability_adjustment"]["next_retry_at"] is None
        assert all(
            row["state"] == "COMPLETED"
            for commitment_type, row in by_type.items()
            if commitment_type != "favorability_adjustment"
        )
        assert completed_at is None
