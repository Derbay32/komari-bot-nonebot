"""AnnouncementDispatchRepository ORM 化后的 PostgreSQL 集成测试。

依赖已执行 ``alembic upgrade head`` 的迁移管理 schema（``KOMARI_TEST_POSTGRES_URL``
门控）。原测试直接注入 asyncpg 连接池，本版本改为与生产一致的公共接口调用
（``claim`` 内部完成初始化），并新增租约过期与对账标记行为覆盖；断言语义
与原测试保持逐条一致。数据准备与清理走同一套 SQLModel 表模型。
"""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from komari_bot.plugins.komari_management.announcement_repository import (
    AnnouncementDispatchRepository,
)
from komari_bot.plugins.komari_management.orm_models import (
    AnnouncementDispatchRow,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")

_D = AnnouncementDispatchRow.__table__


def _configured_database_url() -> str:
    from nonebot import get_driver

    return str(
        getattr(get_driver().config, "sqlalchemy_database_url", "") or ""
    )


def _same_database(left: str, right: str) -> bool:
    left_parsed = urlparse(left)
    right_parsed = urlparse(right)
    return (
        left_parsed.hostname == right_parsed.hostname
        and (left_parsed.port or 5432) == (right_parsed.port or 5432)
        and left_parsed.path == right_parsed.path
    )


async def _reset_shared_orm_engine() -> None:
    """清空 nonebot-plugin-orm 共享引擎连接池（每个测试独立事件循环）。"""
    from nonebot import require

    require("nonebot_plugin_orm")
    import nonebot_plugin_orm as orm_module

    engines = getattr(orm_module, "_engines", None)
    if not engines:
        return
    for engine in list(engines.values()):
        with suppress(Exception):
            await engine.dispose()


def _open_session() -> "AsyncSession":
    from nonebot_plugin_orm import get_session

    return get_session(expire_on_commit=False)


async def _insert_row(
    *,
    request_id: str,
    payload_hash: str,
    status: str = "processing",
    owner_token: str | None = None,
    lease_expires_at: datetime | None = None,
    response_payload: dict[str, object] | None = None,
    completed_at: datetime | None = None,
) -> None:
    session = _open_session()
    try:
        session.add(
            AnnouncementDispatchRow(
                request_id=request_id,
                payload_hash=payload_hash,
                status=status,
                owner_token=owner_token,
                lease_expires_at=lease_expires_at,
                response_payload=response_payload,
                completed_at=completed_at,
            )
        )
        await session.commit()
    finally:
        await session.close()


async def _cleanup(request_ids: list[str]) -> None:
    session = _open_session()
    try:
        await session.execute(
            delete(AnnouncementDispatchRow).where(
                _D.c.request_id.in_(request_ids)
            )
        )
        await session.commit()
    finally:
        await session.close()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_announcement_claim_is_single_owner_and_replayable() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    run_id = uuid4().hex
    request_id = f"integration-request-{run_id}"
    new_request_id = f"new-request-{run_id}"
    unstarted_request_id = f"unstarted-request-{run_id}"
    owner_tokens = [f"worker-{index}-{run_id}" for index in range(1, 6)]
    try:
        first = AnnouncementDispatchRepository()
        second = AnnouncementDispatchRepository()

        async def _claim(
            repository: AnnouncementDispatchRepository,
            owner_token: str,
            *,
            claim_request_id: str = request_id,
            payload_hash: str = "payload-hash",
            cooldown_seconds: int = 0,
        ) -> tuple[str, str]:
            claim = await repository.claim(
                request_id=claim_request_id,
                payload_hash=payload_hash,
                owner_token=owner_token,
                lease_seconds=60,
                cooldown_seconds=cooldown_seconds,
            )
            return owner_token, claim.state

        claims = await asyncio.gather(
            _claim(first, owner_tokens[0]),
            _claim(second, owner_tokens[1]),
        )
        assert sorted(state for _, state in claims) == ["claimed", "in_progress"]
        winner = next(owner for owner, state in claims if state == "claimed")
        assert await first.complete(
            request_id=request_id,
            owner_token=winner,
            response_payload={"total": 1, "results": []},
        )

        replay = await second.claim(
            request_id=request_id,
            payload_hash="payload-hash",
            owner_token=owner_tokens[2],
            lease_seconds=60,
            cooldown_seconds=3600,
        )
        assert replay.state == "replay"
        assert replay.response_payload == {"total": 1, "results": []}

        conflict = await second.claim(
            request_id=request_id,
            payload_hash="different-payload",
            owner_token=owner_tokens[2],
            lease_seconds=60,
            cooldown_seconds=0,
        )
        assert conflict.state == "payload_conflict"

        cooldown = await second.claim(
            request_id=new_request_id,
            payload_hash="new-payload",
            owner_token=owner_tokens[2],
            lease_seconds=60,
            cooldown_seconds=3600,
        )
        assert cooldown.state == "cooldown"
        assert cooldown.remaining_seconds is not None
        assert cooldown.remaining_seconds > 0

        unstarted = await second.claim(
            request_id=unstarted_request_id,
            payload_hash="unstarted-payload",
            owner_token=owner_tokens[3],
            lease_seconds=60,
            cooldown_seconds=0,
        )
        assert unstarted.state == "claimed"
        assert await second.cancel_unstarted(
            request_id=unstarted_request_id,
            owner_token=owner_tokens[3],
        )
        reclaimed = await first.claim(
            request_id=unstarted_request_id,
            payload_hash="unstarted-payload",
            owner_token=owner_tokens[4],
            lease_seconds=60,
            cooldown_seconds=0,
        )
        assert reclaimed.state == "claimed"
    finally:
        await _cleanup([request_id, new_request_id, unstarted_request_id])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_expired_lease_becomes_reconciliation_required_and_blocks_restart() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    run_id = uuid4().hex
    request_id = f"stale-request-{run_id}"
    try:
        await _insert_row(
            request_id=request_id,
            payload_hash="stale-payload",
            status="processing",
            owner_token="dead-worker",
            lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        repository = AnnouncementDispatchRepository()
        claim = await repository.claim(
            request_id=request_id,
            payload_hash="stale-payload",
            owner_token="new-worker",
            lease_seconds=60,
            cooldown_seconds=0,
        )
        assert claim.state == "reconciliation_required"

        # 对账状态禁止自动重发：后续认领永远返回对账标记
        again = await repository.claim(
            request_id=request_id,
            payload_hash="stale-payload",
            owner_token="new-worker",
            lease_seconds=60,
            cooldown_seconds=0,
        )
        assert again.state == "reconciliation_required"

        session = _open_session()
        try:
            row = (
                await session.execute(
                    select(AnnouncementDispatchRow).where(
                        _D.c.request_id == request_id
                    )
                )
            ).scalar_one()
        finally:
            await session.close()
        assert row.status == "reconciliation_required"
        assert row.owner_token is None
        assert row.lease_expires_at is None
    finally:
        await _cleanup([request_id])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_mark_reconciliation_required_is_owner_scoped() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    run_id = uuid4().hex
    request_id = f"recon-request-{run_id}"
    try:
        await _insert_row(
            request_id=request_id,
            payload_hash="recon-payload",
            status="processing",
            owner_token="owner-1",
            lease_expires_at=datetime.now(UTC) + timedelta(seconds=60),
        )
        repository = AnnouncementDispatchRepository()
        await repository.initialize()

        await repository.mark_reconciliation_required(
            request_id=request_id, owner_token="owner-2"
        )

        still_in_progress = await repository.claim(
            request_id=request_id,
            payload_hash="recon-payload",
            owner_token="owner-3",
            lease_seconds=60,
            cooldown_seconds=0,
        )
        assert still_in_progress.state == "in_progress"

        await repository.mark_reconciliation_required(
            request_id=request_id, owner_token="owner-1"
        )
        blocked = await repository.claim(
            request_id=request_id,
            payload_hash="recon-payload",
            owner_token="owner-3",
            lease_seconds=60,
            cooldown_seconds=0,
        )
        assert blocked.state == "reconciliation_required"
    finally:
        await _cleanup([request_id])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_complete_requires_live_lease_and_wrong_owner_fails() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    run_id = uuid4().hex
    request_id = f"complete-request-{run_id}"
    stale_request = f"stale-complete-{run_id}"
    try:
        await _insert_row(
            request_id=request_id,
            payload_hash="payload",
            status="processing",
            owner_token="owner-1",
            lease_expires_at=datetime.now(UTC) + timedelta(seconds=60),
        )
        repository = AnnouncementDispatchRepository()
        await repository.initialize()
        assert (
            await repository.complete(
                request_id=request_id,
                owner_token="owner-2",
                response_payload={"ok": True},
            )
            is False
        )
        assert (
            await repository.complete(
                request_id=request_id,
                owner_token="owner-1",
                response_payload={"ok": True},
            )
            is True
        )
        replay = await repository.claim(
            request_id=request_id,
            payload_hash="payload",
            owner_token="owner-3",
            lease_seconds=60,
            cooldown_seconds=0,
        )
        assert replay.state == "replay"
        assert replay.response_payload == {"ok": True}

        # 租约过期后 complete 被拒绝（回复过期处理中的请求）
        await _insert_row(
            request_id=stale_request,
            payload_hash="stale-payload",
            status="processing",
            owner_token="owner-4",
            lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        assert (
            await repository.complete(
                request_id=stale_request,
                owner_token="owner-4",
                response_payload={"ok": False},
            )
            is False
        )
    finally:
        await _cleanup([request_id, stale_request])
        await _reset_shared_orm_engine()
