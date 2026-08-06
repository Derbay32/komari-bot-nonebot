"""user_ban 仓储 PostgreSQL 集成测试（依赖已 alembic upgrade head 的 schema）。

覆盖原 asyncpg 手写 fake 单测的行为契约：快照一致性读取、作用域原子增删、
缓存 revision 轮询水位、自然解封 outbox 生命周期与分页查询。数据准备与清理
走同一套 SQLModel 表模型，断言全部通过仓储公共接口。
"""

from __future__ import annotations

import os
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, update

from komari_bot.plugins.user_ban.orm_models import (
    UserBanCacheState,
    UserBanNotificationOutbox,
    UserBanRow,
)
from komari_bot.plugins.user_ban.repository import UserBanRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")


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


def _make_user_id() -> str:
    return f"ban-{uuid4().hex}"


async def _insert_bans(rows: list[UserBanRow]) -> None:
    session = _open_session()
    try:
        session.add_all(rows)
        await session.commit()
    finally:
        await session.close()


async def _cleanup_bans(user_ids: list[str]) -> None:
    session = _open_session()
    try:
        await session.execute(
            delete(UserBanRow).where(
                UserBanRow.__table__.c.user_id.in_(user_ids)
            )
        )
        await session.commit()
    finally:
        await session.close()


async def _clear_outbox() -> None:
    """清空 outbox 表（测试库专用；避免失败运行遗留行污染全局 claim 扫描）。"""
    session = _open_session()
    try:
        await session.execute(delete(UserBanNotificationOutbox))
        await session.commit()
    finally:
        await session.close()


async def _cleanup_outbox(notification_ids: list[str]) -> None:
    if not notification_ids:
        return
    session = _open_session()
    try:
        await session.execute(
            delete(UserBanNotificationOutbox).where(
                UserBanNotificationOutbox.__table__.c.notification_id.in_(
                    notification_ids
                )
            )
        )
        await session.commit()
    finally:
        await session.close()


def _row(
    user_id: str,
    scope: str,
    *,
    operator_id: str = "integration",
    reason: str | None = None,
    expires_at: datetime | None = None,
) -> UserBanRow:
    now = datetime.now(UTC)
    return UserBanRow(
        user_id=user_id,
        ban_scope=scope,
        operator_id=operator_id,
        reason=reason,
        expires_at=expires_at,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.skipif(
    not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接"
)
@pytest.mark.asyncio
async def test_load_snapshot_reads_revision_and_active_records_consistently() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip(
            "KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致"
        )
    user_id = _make_user_id()
    expired_user = _make_user_id()
    expires_at = datetime.now(UTC) + timedelta(days=7)
    await _reset_shared_orm_engine()
    await _insert_bans(
        [
            _row(user_id, "chat", reason="集成测试", expires_at=expires_at),
            _row(user_id, "command", reason="永久封禁"),
            _row(
                expired_user,
                "chat",
                reason="已到期",
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            ),
        ]
    )
    try:
        repository = UserBanRepository()
        await repository.initialize()

        snapshot = await repository.load_snapshot()

        assert snapshot.revision >= 1
        by_user = {
            (record.user_id, record.ban_scope): record
            for record in snapshot.records
        }
        chat = by_user[(user_id, "chat")]
        assert chat.reason == "集成测试"
        assert chat.expires_at == expires_at
        assert by_user[(user_id, "command")].is_permanent is True
        assert (expired_user, "chat") not in by_user
    finally:
        await _cleanup_bans([user_id, expired_user])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(
    not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接"
)
@pytest.mark.asyncio
async def test_add_all_scopes_is_atomic_and_returns_current_status() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip(
            "KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致"
        )
    user_id = _make_user_id()
    await _reset_shared_orm_engine()
    try:
        repository = UserBanRepository()
        await repository.initialize()
        revision_before = await repository.get_cache_revision()

        kind, affected, current = await repository.add_scopes(
            user_id=user_id,
            scopes=("chat", "command"),
            operator_id="42",
            reason="刷屏",
            expires_at=None,
        )

        assert kind == "created"
        assert [record.ban_scope for record in affected] == ["chat", "command"]
        assert all(record.reason == "刷屏" for record in affected)
        assert [record.ban_scope for record in current] == ["chat", "command"]
        assert await repository.get_cache_revision() == revision_before + 1
    finally:
        await _cleanup_bans([user_id])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(
    not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接"
)
@pytest.mark.asyncio
async def test_repeated_add_overwrites_expiry_and_reason() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip(
            "KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致"
        )
    user_id = _make_user_id()
    expires_at = datetime.now(UTC) + timedelta(days=7)
    await _reset_shared_orm_engine()
    try:
        repository = UserBanRepository()
        await repository.initialize()
        await repository.add_scopes(
            user_id=user_id,
            scopes=("chat",),
            operator_id="42",
            reason="刷屏",
            expires_at=None,
        )

        kind, affected, current = await repository.add_scopes(
            user_id=user_id,
            scopes=("chat",),
            operator_id="42",
            reason="广告",
            expires_at=expires_at,
        )

        assert kind == "updated"
        assert affected[0].reason == "广告"
        assert affected[0].expires_at == expires_at
        assert current[0].expires_at == expires_at
    finally:
        await _cleanup_bans([user_id])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(
    not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接"
)
@pytest.mark.asyncio
async def test_repeated_identical_permanent_ban_is_idempotent() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip(
            "KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致"
        )
    user_id = _make_user_id()
    await _reset_shared_orm_engine()
    try:
        repository = UserBanRepository()
        await repository.initialize()
        await repository.add_scopes(
            user_id=user_id,
            scopes=("chat",),
            operator_id="42",
            reason=None,
            expires_at=None,
        )
        revision_before = await repository.get_cache_revision()

        kind, affected, current = await repository.add_scopes(
            user_id=user_id,
            scopes=("chat",),
            operator_id="42",
            reason=None,
            expires_at=None,
        )

        assert kind == "unchanged"
        assert affected == ()
        assert current[0].ban_scope == "chat"
        assert await repository.get_cache_revision() == revision_before
    finally:
        await _cleanup_bans([user_id])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(
    not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接"
)
@pytest.mark.asyncio
async def test_changed_ban_fails_when_cache_revision_row_is_missing() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip(
            "KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致"
        )
    user_id = _make_user_id()
    await _reset_shared_orm_engine()
    session = _open_session()
    try:
        await session.execute(
            delete(UserBanCacheState).where(
                UserBanCacheState.__table__.c.singleton_id == 1
            )
        )
        await session.commit()
    finally:
        await session.close()

    repository = UserBanRepository()
    try:
        await repository.initialize()
        with pytest.raises(RuntimeError, match="缓存版本推进失败"):
            await repository.add_scopes(
                user_id=user_id,
                scopes=("chat",),
                operator_id="42",
                reason=None,
                expires_at=None,
            )
    finally:
        await _cleanup_bans([user_id])
        restore = _open_session()
        try:
            restore.add(
                UserBanCacheState(singleton_id=1, revision=1)
            )
            await restore.commit()
        finally:
            await restore.close()
        await _reset_shared_orm_engine()


@pytest.mark.skipif(
    not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接"
)
@pytest.mark.asyncio
async def test_remove_chat_preserves_command_scope_and_returns_removed_record() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip(
            "KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致"
        )
    user_id = _make_user_id()
    await _reset_shared_orm_engine()
    try:
        repository = UserBanRepository()
        await repository.initialize()
        await repository.add_scopes(
            user_id=user_id,
            scopes=("chat", "command"),
            operator_id="42",
            reason="刷屏",
            expires_at=None,
        )
        revision_before = await repository.get_cache_revision()

        removed, current = await repository.remove_scopes(
            user_id=user_id,
            scopes=("chat",),
        )

        assert removed[0].ban_scope == "chat"
        assert removed[0].reason == "刷屏"
        assert [record.ban_scope for record in current] == ["command"]
        assert await repository.get_cache_revision() == revision_before + 1
    finally:
        await _cleanup_bans([user_id])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(
    not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接"
)
@pytest.mark.asyncio
async def test_delete_expired_writes_outbox_and_returns_full_records() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip(
            "KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致"
        )
    user_id = _make_user_id()
    await _reset_shared_orm_engine()
    await _clear_outbox()
    outbox_ids: list[str] = []
    try:
        await _insert_bans(
            [
                _row(
                    user_id,
                    "chat",
                    reason="到期集成测试",
                    expires_at=datetime.now(UTC) - timedelta(seconds=1),
                ),
                _row(user_id, "command"),
            ]
        )
        repository = UserBanRepository()
        await repository.initialize()
        revision_before = await repository.get_cache_revision()

        expired = await repository.delete_expired()

        assert any(
            record.user_id == user_id
            and record.ban_scope == "chat"
            and record.reason == "到期集成测试"
            for record in expired
        )
        assert await repository.get_cache_revision() == revision_before + 1
        snapshot = await repository.load_snapshot()
        assert (user_id, "chat") not in {
            (record.user_id, record.ban_scope)
            for record in snapshot.records
        }
        assert (user_id, "command") in {
            (record.user_id, record.ban_scope)
            for record in snapshot.records
        }

        session = _open_session()
        try:
            pending = (
                await session.execute(
                    select(UserBanNotificationOutbox).where(
                        UserBanNotificationOutbox.__table__.c.user_id
                        == user_id
                    )
                )
            ).scalars().all()
        finally:
            await session.close()
        assert len(pending) == 1
        outbox = pending[0]
        outbox_ids.append(outbox.notification_id)
        assert outbox.notification_kind == "natural_expiry"
        assert outbox.status == "pending"
        assert outbox.records is not None
        assert outbox.records[0]["reason"] == "到期集成测试"
    finally:
        await _cleanup_bans([user_id])
        await _cleanup_outbox(outbox_ids)
        await _reset_shared_orm_engine()


@pytest.mark.skipif(
    not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接"
)
@pytest.mark.asyncio
async def test_expired_notification_claim_ack_and_retry_are_owner_scoped() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip(
            "KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致"
        )
    user_id = _make_user_id()
    await _reset_shared_orm_engine()
    await _clear_outbox()
    outbox_ids: list[str] = []
    try:
        await _insert_bans(
            [
                _row(
                    user_id,
                    "chat",
                    reason="到期测试",
                    expires_at=datetime.now(UTC) - timedelta(seconds=1),
                )
            ]
        )
        repository = UserBanRepository()
        await repository.initialize()
        await repository.delete_expired()

        claimed = await repository.claim_expired_notification(
            owner_token="worker-1",
            lease_seconds=60,
        )
        assert claimed is not None
        outbox_ids.append(claimed.notification_id)
        assert claimed.user_id == user_id
        assert claimed.attempt_count == 1
        assert claimed.records[0].reason == "到期测试"

        wrong_owner = await repository.acknowledge_expired_notification(
            notification_id=claimed.notification_id,
            owner_token="worker-2",
        )
        assert wrong_owner is False

        retried = await repository.retry_expired_notification(
            notification_id=claimed.notification_id,
            owner_token="worker-2",
            error_code="private_message_send_failed",
            retry_delay_seconds=5,
        )
        assert retried is False

        retried = await repository.retry_expired_notification(
            notification_id=claimed.notification_id,
            owner_token="worker-1",
            error_code="private_message_send_failed",
            retry_delay_seconds=5,
        )
        assert retried is True

        second_claim = await repository.claim_expired_notification(
            owner_token="worker-3",
            lease_seconds=60,
        )
        assert second_claim is None

        acknowledged = await repository.acknowledge_expired_notification(
            notification_id=claimed.notification_id,
            owner_token="worker-3",
        )
        assert acknowledged is False

        # 重试后状态回到 pending：必须先把 available_at 拨回过去并重新
        # 认领（processing + 持有租约），原持有者才能完成确认。
        session = _open_session()
        try:
            await session.execute(
                update(UserBanNotificationOutbox)
                .where(
                    UserBanNotificationOutbox.__table__.c.notification_id
                    == claimed.notification_id
                )
                .values(available_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()
        finally:
            await session.close()
        reclaimed = await repository.claim_expired_notification(
            owner_token="worker-1",
            lease_seconds=60,
        )
        assert reclaimed is not None
        assert reclaimed.notification_id == claimed.notification_id

        acknowledged = await repository.acknowledge_expired_notification(
            notification_id=claimed.notification_id,
            owner_token="worker-1",
        )
        assert acknowledged is True

        session = _open_session()
        try:
            outbox = (
                await session.execute(
                    select(UserBanNotificationOutbox).where(
                        UserBanNotificationOutbox.__table__.c.notification_id
                        == claimed.notification_id
                    )
                )
            ).scalar_one()
        finally:
            await session.close()
        assert outbox.status == "sent"
        assert outbox.records is None
        assert outbox.owner_token is None
        assert outbox.sent_at is not None
    finally:
        await _cleanup_bans([user_id])
        await _cleanup_outbox(outbox_ids)
        await _reset_shared_orm_engine()


@pytest.mark.skipif(
    not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接"
)
@pytest.mark.asyncio
async def test_list_statuses_pages_users_and_returns_all_their_scopes() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip(
            "KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致"
        )
    user_one = _make_user_id()
    user_two = _make_user_id()
    expired_user = _make_user_id()
    await _reset_shared_orm_engine()
    await _insert_bans(
        [
            _row(user_one, "chat", reason="聊天封禁"),
            _row(user_one, "command", reason="命令封禁"),
            _row(user_two, "command", reason="仅命令"),
            _row(
                expired_user,
                "chat",
                reason="已到期",
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            ),
        ]
    )
    try:
        repository = UserBanRepository()
        await repository.initialize()

        statuses, total = await repository.list_statuses(
            scope="chat",
            limit=20,
            offset=0,
        )

        assert total == 1
        assert len(statuses) == 1
        assert statuses[0].user_id == user_one
        assert statuses[0].active_scopes == {"chat", "command"}

        statuses_all, total_all = await repository.list_statuses(
            scope=None,
            limit=1,
            offset=0,
        )
        assert total_all == 2
        assert len(statuses_all) == 1
        statuses_rest, _ = await repository.list_statuses(
            scope=None,
            limit=1,
            offset=1,
        )
        assert len(statuses_rest) == 1
        assert {statuses_all[0].user_id, statuses_rest[0].user_id} == {
            user_one,
            user_two,
        }
    finally:
        await _cleanup_bans([user_one, user_two, expired_user])
        await _reset_shared_orm_engine()
