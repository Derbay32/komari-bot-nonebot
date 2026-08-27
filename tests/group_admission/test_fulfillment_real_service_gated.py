"""AC8 — 回复履约真实存储门控验收：PostgreSQL 基础不可分保证 + 主动预占 Redis 幂等。

- PostgreSQL 用例（``KOMARI_TEST_POSTGRES_URL`` + ``SQLALCHEMY_DATABASE_URL``
  未指向同一真实库时 ``skipif`` 跳过）：真实 ``ReplyFulfillmentRepository``
  的 CAS 租约串行、同事件重复准备幂等、崩溃恢复窗口可再领取。
- Redis 用例（``KOMARI_TEST_REDIS_URL`` 未配置则 ``skipif`` 跳过）：驱动真实
  ``ProactiveReservationService`` 的 Lua 原语，验证释放幂等、confirm 幂等、
  已确认名额不因释放撤销、pending 短 TTL 纯淘汰。

本地可用环境（写进用例文档注释）：PG DSN 取主仓 ``.env.dev`` 的
``SQLALCHEMY_DATABASE_URL``；Redis 用 ``redis://:<密码>@192.168.3.107:6379/15``，
密码读主仓 ``.env.dev`` 的 ``REDIS_PASSWORD``。运行时把
``KOMARI_TEST_POSTGRES_URL``/``SQLALCHEMY_DATABASE_URL``/``KOMARI_TEST_REDIS_URL``
三个门控变量导出即可。

存储 adapter 策略无关，准入时点由编排层裁决（本文件不测 adjudicate 接入）。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
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
    """发送前崩溃恢复的不可分保证：身份幂等、DELIVERED 后 CAS 租约串行。"""
    if not DATABASE_CONFIGURED:
        pytest.skip("未指向同一真实 PG 库")
    url = POSTGRES_URL.replace("postgresql+asyncpg://", "postgresql://")
    pool = await asyncpg.create_pool(url, min_size=1)
    fid = f"gated-{uuid4().hex[:12]}"
    try:
        repo = ReplyFulfillmentRepository(pool)
        draft = _draft(fid)
        assert await repo.prepare(draft) is True
        # 按生产真实路径推进：NOT_STARTED -> PENDING_CONFIRMATION -> DELIVERED
        # （父记录须 DELIVERED 且存在 PENDING 子承诺才处于可领取租约状态）
        assert await repo.mark_send_started(fid) is True
        assert await repo.mark_delivered(fid) is True
        # 幂等：同事件已有持久身份，绝不重复准备（防重发基石）
        assert await repo.prepare(_draft(fid)) is False
        assert await repo.has_fulfillment(fid) is True
        # CAS 租约：DELIVERED 父可领，同一时刻另一 Owner 的第二次领取被拒
        first = await repo.claim_lease(fid, owner_token="tok-a", lease_seconds=30)
        second = await repo.claim_lease(fid, owner_token="tok-c", lease_seconds=30)
        assert first is not None, "DELIVERED 含 PENDING 子承诺时应可领取租约"
        assert second is None, "CAS 租约应串行：同时刻另一 Owner 不得获租"
        assert await repo.release_lease(fid, owner_token="tok-a") is True
        # 崩溃恢复窗口：释放后另一 Owner 可重新领取（只补父完成不重复子项）
        retaken = await repo.claim_lease(fid, owner_token="tok-c", lease_seconds=30)
        assert retaken is not None, "租约释放后另一 Owner 应可领取"
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM komari_chat_reply_fulfillments WHERE fulfillment_id = $1",
                fid,
            )
        await pool.close()


REDIS_URL = os.getenv("KOMARI_TEST_REDIS_URL", "")


def _slots_key(group_id: str) -> str:
    from komari_bot.plugins.komari_chat.services.proactive_reservation import (
        _PROACTIVE_SLOTS_KEY_PREFIX,
    )

    return f"{_PROACTIVE_SLOTS_KEY_PREFIX}{group_id}"


async def _redis_client() -> Any:
    import redis.asyncio as aioredis

    return aioredis.from_url(REDIS_URL, decode_responses=True)


def _service(client: Any, **overrides: object) -> Any:
    from komari_bot.plugins.komari_chat.services import proactive_reservation as pr

    def _cfg() -> Any:
        from types import SimpleNamespace

        base: dict[str, Any] = {
            "proactive_cooldown": 30,
            "proactive_max_per_hour": 1000,
            "proactive_reservation_ttl_seconds": 30,
        }
        base.update(overrides)
        return SimpleNamespace(**base)

    pr.get_config = _cfg
    return pr.ProactiveReservationService(client)


async def test_real_redis_release_idempotent_and_confirmed_not_revoked() -> None:
    """预占释放幂等，已确认名额绝不因 release 撤销。"""
    if not REDIS_URL:
        pytest.skip("未配置真实 Redis")
    client = await _redis_client()
    service = _service(client)
    gid = f"tsk228r-{uuid4().hex[:10]}"
    slots = _slots_key(gid)
    try:
        rid = f"res-{uuid4().hex[:10]}"
        lease = await service.reserve(gid, rid)
        handoff = await lease.handoff()
        assert await handoff.release() is True, "首次释放应移除 pending"
        assert await handoff.release() is False, "重复释放应幂等"
        assert await client.zscore(slots, f"pending:{rid}") is None
    finally:
        await client.flushdb()
        await client.aclose()


async def test_real_redis_confirm_idempotent_and_confirmed_survives_release() -> None:
    """confirm 幂等，已确认名额持续存在且 release 不撤销。"""
    if not REDIS_URL:
        pytest.skip("未配置真实 Redis")
    client = await _redis_client()
    service = _service(client)
    gid = f"tsk228c-{uuid4().hex[:10]}"
    slots = _slots_key(gid)
    try:
        rid = f"res-{uuid4().hex[:10]}"
        await service.reserve(gid, rid)
        await service.confirm(gid, rid, cooldown_seconds=60)
        await service.confirm(gid, rid, cooldown_seconds=60)
        assert await client.zscore(slots, f"confirmed:{rid}") is not None
        await service.release(gid, rid)
        assert await client.zscore(slots, f"confirmed:{rid}") is not None
    finally:
        await client.flushdb()
        await client.aclose()


async def test_real_redis_pending_expires_by_ttl() -> None:
    """pending 预占超过短 TTL 后按纯 TTL 淘汰，handoff 抛 ReservationLostError。"""
    if not REDIS_URL:
        pytest.skip("未配置真实 Redis")
    await asyncio.sleep(0)
    from komari_bot.plugins.komari_chat.services.proactive_reservation import (
        ReservationLostError,
    )

    client = await _redis_client()
    service = _service(client, proactive_reservation_ttl_seconds=1)
    gid = f"tsk228t-{uuid4().hex[:10]}"
    rid = f"res-{uuid4().hex[:10]}"
    try:
        lease = await service.reserve(gid, rid)
        await asyncio.sleep(1.5)
        with pytest.raises(ReservationLostError):
            await lease.handoff()
    finally:
        await client.flushdb()
        await client.aclose()
