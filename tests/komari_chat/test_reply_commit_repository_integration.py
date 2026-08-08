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


async def test_claim_pending_batch_limit_order_and_skip_locked() -> None:
    """批量领取：limit 生效、按时间+operation_id 稳定排序、SKIP LOCKED 跳过并发锁定行。"""
    run_id = uuid4().hex
    operation_ids = [f"pending-{i}-{run_id}" for i in range(5)]
    pool = await asyncpg.create_pool(_asyncpg_url(), min_size=1, max_size=2)
    try:
        repository = ReplyCommitRepository(pool)
        for operation_id in operation_ids:
            payload = _payload(operation_id)
            assert await repository.prepare(payload) is True
            assert await repository.mark_delivered(operation_id) is True
        # 错开 delivered_at（下标越小越老），验证候选按
        # COALESCE(next_retry_at, delivered_at, created_at) 升序稳定领取
        async with pool.acquire() as connection:
            for index, operation_id in enumerate(operation_ids):
                await connection.execute(
                    """
                    UPDATE komari_chat_reply_commit_outbox
                    SET delivered_at = NOW() - ($2 * INTERVAL '100 seconds')
                    WHERE operation_id = $1
                    """,
                    operation_id,
                    5 - index,
                )

        # limit 生效：非正 limit 直接短路返回空
        assert (
            await repository.claim_pending(
                owner_token="worker-1", limit=0, lease_seconds=60
            )
            == []
        )
        assert (
            await repository.claim_pending(
                owner_token="worker-1", limit=-1, lease_seconds=60
            )
            == []
        )
        # limit 生效：只领取最老的两条，且返回顺序与排序键一致
        claimed = await repository.claim_pending(
            owner_token="worker-1", limit=2, lease_seconds=60
        )
        assert [row["operation_id"] for row in claimed] == operation_ids[:2]
        assert [row["status"] for row in claimed] == ["PROCESSING", "PROCESSING"]
        assert [row["lease_owner"] for row in claimed] == ["worker-1", "worker-1"]
        assert [row["attempt_count"] for row in claimed] == [1, 1]

        # SKIP LOCKED：竞争者事务锁住下一条候选时，批量领取跳过而非阻塞
        locker = await pool.acquire()
        locker_transaction = locker.transaction()
        try:
            await locker_transaction.start()
            await locker.execute(
                """
                SELECT operation_id
                FROM komari_chat_reply_commit_outbox
                WHERE operation_id = $1
                FOR UPDATE
                """,
                operation_ids[2],
            )
            claimed_skipped = await repository.claim_pending(
                owner_token="worker-2", limit=10, lease_seconds=60
            )
            assert [row["operation_id"] for row in claimed_skipped] == (
                operation_ids[3:]
            )
        finally:
            await locker_transaction.rollback()
            await pool.release(locker)

        # 锁释放后，之前被跳过的行成为新的领取候选
        claimed_last = await repository.claim_pending(
            owner_token="worker-3", limit=10, lease_seconds=60
        )
        assert [row["operation_id"] for row in claimed_last] == [operation_ids[2]]
        assert claimed_last[0]["attempt_count"] == 1
        assert claimed_last[0]["lease_owner"] == "worker-3"
    finally:
        async with pool.acquire() as connection:
            await connection.execute(
                """
                DELETE FROM komari_chat_reply_commit_outbox
                WHERE operation_id = ANY($1::text[])
                """,
                operation_ids,
            )
        await pool.close()


async def test_claim_pending_reclaims_expired_leases() -> None:
    """过期租约的 PROCESSING 记录可作为回收候选被重新认领。"""
    run_id = uuid4().hex
    operation_id = f"expired-lease-{run_id}"
    pool = await asyncpg.create_pool(_asyncpg_url(), min_size=1, max_size=2)
    try:
        repository = ReplyCommitRepository(pool)
        payload = _payload(operation_id)
        assert await repository.prepare(payload) is True
        assert await repository.mark_delivered(operation_id) is True
        assert (
            await repository.claim_operation(
                operation_id,
                owner_token="worker-1",
                lease_seconds=60,
            )
            is not None
        )

        # 租约未过期：PROCESSING 记录不是批量领取候选
        claimed = await repository.claim_pending(
            owner_token="worker-2", limit=10, lease_seconds=60
        )
        assert operation_id not in {row["operation_id"] for row in claimed}

        # 租约过期：可被其他 worker 回收并重新认领
        async with pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE komari_chat_reply_commit_outbox
                SET lease_expires_at = NOW() - INTERVAL '1 second'
                WHERE operation_id = $1
                """,
                operation_id,
            )
        reclaimed = await repository.claim_pending(
            owner_token="worker-2", limit=10, lease_seconds=60
        )
        reclaimed_row = next(
            row for row in reclaimed if row["operation_id"] == operation_id
        )
        assert reclaimed_row["status"] == "PROCESSING"
        assert reclaimed_row["lease_owner"] == "worker-2"
        assert reclaimed_row["attempt_count"] == 2
    finally:
        async with pool.acquire() as connection:
            await connection.execute(
                """
                DELETE FROM komari_chat_reply_commit_outbox
                WHERE operation_id = $1
                """,
                operation_id,
            )
        await pool.close()


async def test_renew_lease_rejects_non_owner() -> None:
    """续租只对持有租约的 owner 生效，错误 owner 与未领取记录被拒绝。"""
    run_id = uuid4().hex
    operation_id = f"renew-lease-{run_id}"
    other_operation_id = f"renew-lease-other-{run_id}"
    pool = await asyncpg.create_pool(_asyncpg_url(), min_size=1, max_size=2)
    try:
        repository = ReplyCommitRepository(pool)
        payload = _payload(operation_id)
        assert await repository.prepare(payload) is True
        assert await repository.mark_delivered(operation_id) is True
        assert (
            await repository.claim_operation(
                operation_id,
                owner_token="worker-1",
                lease_seconds=60,
            )
            is not None
        )
        async with pool.acquire() as connection:
            lease_before = await connection.fetchval(
                """
                SELECT lease_expires_at
                FROM komari_chat_reply_commit_outbox
                WHERE operation_id = $1
                """,
                operation_id,
            )

        # 持有 owner 续租成功，租约被延长
        assert (
            await repository.renew_lease(
                operation_id,
                owner_token="worker-1",
                lease_seconds=60,
            )
            is True
        )
        async with pool.acquire() as connection:
            lease_after = await connection.fetchval(
                """
                SELECT lease_expires_at
                FROM komari_chat_reply_commit_outbox
                WHERE operation_id = $1
                """,
                operation_id,
            )
        assert lease_after > lease_before

        # 非持有 owner 续租被拒绝，租约保持不变
        assert (
            await repository.renew_lease(
                operation_id,
                owner_token="worker-2",
                lease_seconds=60,
            )
            is False
        )
        async with pool.acquire() as connection:
            lease_after_reject = await connection.fetchval(
                """
                SELECT lease_expires_at
                FROM komari_chat_reply_commit_outbox
                WHERE operation_id = $1
                """,
                operation_id,
            )
        assert lease_after_reject == lease_after

        # 未进入 PROCESSING 的记录同样拒绝续租
        other_payload = _payload(other_operation_id)
        assert await repository.prepare(other_payload) is True
        assert await repository.mark_delivered(other_operation_id) is True
        assert (
            await repository.renew_lease(
                other_operation_id,
                owner_token="worker-1",
                lease_seconds=60,
            )
            is False
        )
    finally:
        async with pool.acquire() as connection:
            await connection.execute(
                """
                DELETE FROM komari_chat_reply_commit_outbox
                WHERE operation_id = ANY($1::text[])
                """,
                [operation_id, other_operation_id],
            )
        await pool.close()


async def test_mark_failure_backs_off_and_returns_to_delivered() -> None:
    """未超限失败：回到 DELIVERED，next_retry_at 按指数退避推进。"""
    run_id = uuid4().hex
    operation_id = f"fail-backoff-{run_id}"
    pool = await asyncpg.create_pool(_asyncpg_url(), min_size=1, max_size=2)
    try:
        repository = ReplyCommitRepository(pool)
        payload = _payload(operation_id)
        assert await repository.prepare(payload) is True
        assert await repository.mark_delivered(operation_id) is True
        assert (
            await repository.claim_operation(
                operation_id,
                owner_token="worker-1",
                lease_seconds=60,
            )
            is not None
        )

        # 非持有 owner 标记失败被拒绝，不影响当前状态
        assert (
            await repository.mark_failure(
                operation_id,
                owner_token="worker-2",
                error_code="stolen",
                max_attempts=3,
                retry_base_seconds=10,
            )
            is None
        )
        # 持有 owner 第一次失败：attempt=1 < max_attempts=3，
        # 回到 DELIVERED，退避 10s = 10 * 2^0
        assert (
            await repository.mark_failure(
                operation_id,
                owner_token="worker-1",
                error_code="network_error",
                max_attempts=3,
                retry_base_seconds=10,
            )
            == "DELIVERED"
        )
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT status, last_error_code, lease_owner,
                       EXTRACT(EPOCH FROM (next_retry_at - NOW()))::float8
                           AS delay_seconds
                FROM komari_chat_reply_commit_outbox
                WHERE operation_id = $1
                """,
                operation_id,
            )
        assert row["status"] == "DELIVERED"
        assert row["last_error_code"] == "network_error"
        assert row["lease_owner"] is None
        assert 8 <= row["delay_seconds"] <= 11

        # 第二次失败：退避翻倍为 20s = 10 * 2^1
        async with pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE komari_chat_reply_commit_outbox
                SET next_retry_at = NOW() - INTERVAL '1 second'
                WHERE operation_id = $1
                """,
                operation_id,
            )
        assert (
            await repository.claim_operation(
                operation_id,
                owner_token="worker-1",
                lease_seconds=60,
            )
            is not None
        )
        assert (
            await repository.mark_failure(
                operation_id,
                owner_token="worker-1",
                error_code="network_error",
                max_attempts=3,
                retry_base_seconds=10,
            )
            == "DELIVERED"
        )
        async with pool.acquire() as connection:
            delay_seconds = await connection.fetchval(
                """
                SELECT EXTRACT(EPOCH FROM (next_retry_at - NOW()))::float8
                FROM komari_chat_reply_commit_outbox
                WHERE operation_id = $1
                """,
                operation_id,
            )
        assert 18 <= delay_seconds <= 21
    finally:
        async with pool.acquire() as connection:
            await connection.execute(
                """
                DELETE FROM komari_chat_reply_commit_outbox
                WHERE operation_id = $1
                """,
                operation_id,
            )
        await pool.close()


async def test_mark_failure_exhausts_attempts_to_failed() -> None:
    """失败次数达到上限：转为 FAILED 且不再排期、不可领取。"""
    run_id = uuid4().hex
    operation_id = f"fail-exhaust-{run_id}"
    pool = await asyncpg.create_pool(_asyncpg_url(), min_size=1, max_size=2)
    try:
        repository = ReplyCommitRepository(pool)
        payload = _payload(operation_id)
        assert await repository.prepare(payload) is True
        assert await repository.mark_delivered(operation_id) is True

        # 第一次失败：attempt=1 < max_attempts=2，仍回到 DELIVERED
        assert (
            await repository.claim_operation(
                operation_id,
                owner_token="worker-1",
                lease_seconds=60,
            )
            is not None
        )
        assert (
            await repository.mark_failure(
                operation_id,
                owner_token="worker-1",
                error_code="transient",
                max_attempts=2,
                retry_base_seconds=10,
            )
            == "DELIVERED"
        )
        # 第二次失败：attempt=2 >= max_attempts，转为 FAILED，next_retry_at 置空
        async with pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE komari_chat_reply_commit_outbox
                SET next_retry_at = NOW() - INTERVAL '1 second'
                WHERE operation_id = $1
                """,
                operation_id,
            )
        assert (
            await repository.claim_operation(
                operation_id,
                owner_token="worker-1",
                lease_seconds=60,
            )
            is not None
        )
        assert (
            await repository.mark_failure(
                operation_id,
                owner_token="worker-1",
                error_code="transient",
                max_attempts=2,
                retry_base_seconds=10,
            )
            == "FAILED"
        )
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT status, next_retry_at, last_error_code, lease_owner
                FROM komari_chat_reply_commit_outbox
                WHERE operation_id = $1
                """,
                operation_id,
            )
        assert row["status"] == "FAILED"
        assert row["next_retry_at"] is None
        assert row["last_error_code"] == "transient"
        assert row["lease_owner"] is None
        # FAILED 记录不再是领取候选
        claimed = await repository.claim_pending(
            owner_token="worker-1", limit=10, lease_seconds=60
        )
        assert operation_id not in {row["operation_id"] for row in claimed}
    finally:
        async with pool.acquire() as connection:
            await connection.execute(
                """
                DELETE FROM komari_chat_reply_commit_outbox
                WHERE operation_id = $1
                """,
                operation_id,
            )
        await pool.close()


async def test_cleanup_tombstones_only_removes_completed_cancelled() -> None:
    """清理只删除过期的 COMPLETED/CANCELLED，FAILED 永不清理。"""
    run_id = uuid4().hex
    completed_old = f"tombstone-completed-old-{run_id}"
    cancelled_old = f"tombstone-cancelled-old-{run_id}"
    failed_old = f"tombstone-failed-old-{run_id}"
    completed_fresh = f"tombstone-completed-fresh-{run_id}"
    all_operation_ids = [completed_old, cancelled_old, failed_old, completed_fresh]
    pool = await asyncpg.create_pool(_asyncpg_url(), min_size=1, max_size=2)
    try:
        repository = ReplyCommitRepository(pool)

        async def _complete_operation(operation_id: str) -> None:
            payload = _payload(operation_id)
            assert await repository.prepare(payload) is True
            assert await repository.mark_delivered(operation_id) is True
            assert (
                await repository.claim_operation(
                    operation_id,
                    owner_token="worker-1",
                    lease_seconds=60,
                )
                is not None
            )
            for step in (
                "proactive_confirmed",
                "favorability_applied",
                "ai_history_stored",
                "interaction_stored",
            ):
                assert await repository.mark_step(
                    operation_id,
                    owner_token="worker-1",
                    step=step,
                )
            assert await repository.complete(operation_id, owner_token="worker-1")

        await _complete_operation(completed_old)
        await _complete_operation(completed_fresh)

        cancelled_payload = _payload(cancelled_old)
        assert await repository.prepare(cancelled_payload) is True
        assert await repository.cancel_prepared(cancelled_old) is True

        failed_payload = _payload(failed_old)
        assert await repository.prepare(failed_payload) is True
        assert await repository.mark_delivered(failed_old) is True
        assert (
            await repository.claim_operation(
                failed_old,
                owner_token="worker-1",
                lease_seconds=60,
            )
            is not None
        )
        assert (
            await repository.mark_failure(
                failed_old,
                owner_token="worker-1",
                error_code="exhausted",
                max_attempts=1,
                retry_base_seconds=10,
            )
            == "FAILED"
        )

        # 把三条终态记录的时间戳推回 30 天前，另一条 COMPLETED 保持新鲜
        async with pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE komari_chat_reply_commit_outbox
                SET updated_at = NOW() - INTERVAL '30 days'
                WHERE operation_id = ANY($1::text[])
                """,
                [completed_old, cancelled_old, failed_old],
            )

        # 只删除过期的 COMPLETED/CANCELLED 两条，FAILED 与新鲜 tombstone 保留
        deleted = await repository.cleanup_tombstones(retention_days=1)
        assert deleted == 2
        async with pool.acquire() as connection:
            remaining = [
                row["operation_id"]
                for row in await connection.fetch(
                    """
                    SELECT operation_id
                    FROM komari_chat_reply_commit_outbox
                    WHERE operation_id = ANY($1::text[])
                    ORDER BY operation_id
                    """,
                    all_operation_ids,
                )
            ]
        assert remaining == sorted([failed_old, completed_fresh])

        # FAILED 永不清理：即使 updated_at 更旧也不会被删除
        async with pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE komari_chat_reply_commit_outbox
                SET updated_at = NOW() - INTERVAL '30 days'
                WHERE operation_id = $1
                """,
                failed_old,
            )
        assert await repository.cleanup_tombstones(retention_days=1) == 0
        async with pool.acquire() as connection:
            still_exists = await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM komari_chat_reply_commit_outbox
                    WHERE operation_id = ANY($1::text[])
                )
                """,
                [failed_old, completed_fresh],
            )
        assert still_exists is True
    finally:
        async with pool.acquire() as connection:
            await connection.execute(
                """
                DELETE FROM komari_chat_reply_commit_outbox
                WHERE operation_id = ANY($1::text[])
                """,
                all_operation_ids,
            )
        await pool.close()
