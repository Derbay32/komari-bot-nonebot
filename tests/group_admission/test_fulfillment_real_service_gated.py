"""AC8 — 回复履约真实 PostgreSQL CAS/租约/幂等/崩溃恢复门控验收。

``KOMARI_TEST_POSTGRES_URL`` 未指向真实库时 ``skipif`` 跳过（缺库跳过合法；
本地可用值见 .env.dev）。驱动真实 ``ReplyFulfillmentRepository`` 验证发送前
崩溃恢复所需的不可分保证：CAS 租约同 Owner 同一时点至多领一次、已持久化身
份绝不被同事件重复准备（幂等基石）、送达推进可持久化。存储 adapter 策略无
感，准入时点由编排层裁决（本文件不测 adjudicate 接入）。
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import pytest

from komari_bot.plugins.komari_chat.reply_fulfillment_domain import (
    AssistantReplyHistoryPayload,
    FavorabilityAdjustmentPayload,
    ProactiveReplyConfirmationPayload,
    ReplyCommitmentInput,
    ReplyFulfillmentDraft,
)
from komari_bot.plugins.komari_chat.repositories.reply_fulfillment_repository import (
    ReplyFulfillmentRepository,
)

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")
SQLALCHEMY_URL = os.getenv("SQLALCHEMY_DATABASE_URL", "")


def _db_identity(url: str) -> tuple[str, None | int, int]:
    parsed = urlsplit(url.replace("postgresql+asyncpg://", "postgresql://"))
    return parsed.hostname or "", parsed.port, len(parsed.path)


DATABASE_CONFIGURED = bool(POSTGRES_URL and SQLALCHEMY_URL) and (
    _db_identity(POSTGRES_URL) == _db_identity(SQLALCHEMY_URL)
)

pytestmark = pytest.mark.group_admission_service


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
        reply_target_message_id="target-1",
        reply_content="正文",
        commitments=(
            ReplyCommitmentInput(
                commitment_type="proactive_reply_confirmation",
                payload=ProactiveReplyConfirmationPayload(
                    group_id="group-1", reservation_id="r-1", cooldown_seconds=300),
            ),
            ReplyCommitmentInput(
                commitment_type="favorability_adjustment",
                payload=FavorabilityAdjustmentPayload(
                    user_id="user-1", delta=1, reason="互动"),
            ),
            ReplyCommitmentInput(
                commitment_type="assistant_reply_history",
                payload=AssistantReplyHistoryPayload(
                    group_id="group-1", bot_nickname="小鞠",
                    reply_content="正文", reply_timestamp=2.0),
            ),
        ),
    )


async def test_real_pg_cas_lease_and_crash_recovery() -> None:
    """发送前崩溃：身份幂等阻塞重复准备，CAS 租约至多领一次，送达可推进。"""
    if not DATABASE_CONFIGURED:
        pytest.skip("未指向同一真实 PG 库")
    url = POSTGRES_URL.replace("postgresql+asyncpg://", "postgresql://")
    pool = await asyncpg.create_pool(url, min_size=1)
    fid = f"gated-{uuid4().hex[:12]}"
    try:
        repo = ReplyFulfillmentRepository(pool)
        draft = _draft(fid)
        assert await repo.prepare(draft) is True
        assert await repo.mark_send_started(fid) is True
        assert await repo.prepare(_draft(fid)) is False
        assert await repo.has_fulfillment(fid) is True
        first = await repo.claim_lease(fid, owner_token="tok-a", lease_seconds=30)
        second = await repo.claim_lease(fid, owner_token="tok-c", lease_seconds=30)
        assert first is not None and second is None, "CAS 租约应串行"
        assert await repo.release_lease(fid, owner_token="tok-a") is True
        assert await repo.mark_delivered(fid) is True
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM komari_chat_reply_fulfillments WHERE fulfillment_id = $1",
                fid,
            )
        await pool.close()
