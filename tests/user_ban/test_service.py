"""user_ban 服务和缓存测试。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from komari_bot.plugins.user_ban.models import BanRecord, BanScope
from komari_bot.plugins.user_ban.repository import BanCacheSnapshot
from komari_bot.plugins.user_ban.service import (
    BanServiceUnavailableError,
    UserBanService,
)


def _record(
    user_id: str,
    scope: BanScope,
    *,
    reason: str | None = None,
    expires_at: datetime | None = None,
) -> BanRecord:
    timestamp = datetime(2026, 7, 13, 20, 0, tzinfo=UTC)
    return BanRecord(
        user_id=user_id,
        ban_scope=scope,
        operator_id="42",
        reason=reason,
        expires_at=expires_at,
        created_at=timestamp,
        updated_at=timestamp,
    )


class _Repository:
    def __init__(self, records: tuple[BanRecord, ...] = ()) -> None:
        self.records = list(records)
        self.initialize_calls = 0
        self.load_calls = 0
        self.revision_calls = 0
        self.revision = 1
        self.snapshot_delay = 0.0
        self.closed = False
        self.load_error: Exception | None = None
        self.revision_error: Exception | None = None

    async def initialize(self) -> None:
        self.initialize_calls += 1

    async def close(self) -> None:
        self.closed = True

    async def get_cache_revision(self) -> int:
        self.revision_calls += 1
        if self.revision_error is not None:
            raise self.revision_error
        return self.revision

    async def load_snapshot(self) -> BanCacheSnapshot:
        self.load_calls += 1
        if self.snapshot_delay:
            await asyncio.sleep(self.snapshot_delay)
        if self.load_error is not None:
            raise self.load_error
        return BanCacheSnapshot(
            revision=self.revision,
            records=tuple(record for record in self.records if record.is_active),
        )

    async def add_scopes(
        self,
        *,
        user_id: str,
        scopes: tuple[BanScope, ...],
        operator_id: str,
        reason: str | None,
        expires_at: datetime | None,
    ):
        existing = {
            record.ban_scope: record
            for record in self.records
            if record.user_id == user_id and record.ban_scope in scopes
        }
        affected: list[BanRecord] = []
        for scope in scopes:
            previous = existing.get(scope)
            if (
                previous is not None
                and previous.operator_id == operator_id
                and previous.reason == reason
                and previous.expires_at == expires_at
            ):
                continue
            if previous is None:
                current = _record(
                    user_id,
                    scope,
                    reason=reason,
                    expires_at=expires_at,
                )
            else:
                current = replace(
                    previous,
                    operator_id=operator_id,
                    reason=reason,
                    expires_at=expires_at,
                    updated_at=datetime.now(UTC),
                )
                self.records.remove(previous)
            self.records.append(current)
            affected.append(current)

        if not affected:
            kind = "unchanged"
        elif existing:
            kind = "updated"
        else:
            kind = "created"
        if affected:
            self.revision += 1
        current_records = tuple(
            record
            for record in self.records
            if record.user_id == user_id and record.is_active
        )
        return kind, tuple(affected), current_records

    async def remove_scopes(
        self,
        *,
        user_id: str,
        scopes: tuple[BanScope, ...],
    ):
        removed = tuple(
            record
            for record in self.records
            if record.user_id == user_id
            and record.ban_scope in scopes
            and record.is_active
        )
        self.records = [record for record in self.records if record not in removed]
        if removed:
            self.revision += 1
        current = tuple(
            record
            for record in self.records
            if record.user_id == user_id and record.is_active
        )
        return removed, current

    async def delete_expired(self) -> tuple[BanRecord, ...]:
        expired = tuple(record for record in self.records if not record.is_active)
        self.records = [record for record in self.records if record not in expired]
        if expired:
            self.revision += 1
        return expired

    async def list_statuses(self, **_kwargs: object):
        raise AssertionError("当前测试不应调用分页查询")


@pytest.mark.asyncio
async def test_cache_is_reused_within_ttl() -> None:
    repository = _Repository((_record("10086", "chat"),))
    service = UserBanService(repository, cache_ttl_seconds=60)  # type: ignore[arg-type]

    assert await service.is_user_banned("10086", "chat") is True
    assert await service.is_user_banned("10086", "chat") is True
    assert repository.load_calls == 1


@pytest.mark.asyncio
async def test_expired_ttl_checks_revision_without_reloading_unchanged_snapshot() -> None:
    repository = _Repository((_record("10086", "chat"),))
    service = UserBanService(repository, cache_ttl_seconds=0)  # type: ignore[arg-type]

    assert await service.is_user_banned("10086", "chat") is True
    assert await service.is_user_banned("10086", "chat") is True

    assert repository.load_calls == 1
    assert repository.revision_calls == 1


@pytest.mark.asyncio
async def test_changed_revision_reloads_cross_worker_snapshot() -> None:
    repository = _Repository((_record("10086", "chat"),))
    service = UserBanService(repository, cache_ttl_seconds=0)  # type: ignore[arg-type]
    assert await service.is_user_banned("10086", "chat") is True

    repository.records.append(_record("20000", "command"))
    repository.revision += 1

    assert await service.is_user_banned("20000", "command") is True
    assert repository.load_calls == 2
    assert repository.revision_calls == 1


@pytest.mark.asyncio
async def test_concurrent_initialize_builds_only_one_snapshot() -> None:
    repository = _Repository((_record("10086", "chat"),))
    repository.snapshot_delay = 0.01
    service = UserBanService(repository, cache_ttl_seconds=60)  # type: ignore[arg-type]

    await asyncio.gather(*(service.initialize() for _ in range(20)))

    assert repository.initialize_calls == 1
    assert repository.load_calls == 1


@pytest.mark.asyncio
async def test_expired_cache_record_stops_blocking_immediately() -> None:
    expired = _record(
        "10086",
        "chat",
        expires_at=datetime.now(UTC) - timedelta(milliseconds=1),
    )
    repository = _Repository((expired,))
    service = UserBanService(repository, cache_ttl_seconds=60)  # type: ignore[arg-type]
    service._cache = {"10086": service._build_cache((expired,))["10086"]}
    service._refreshed_at = 0.0

    assert await service.is_user_banned("10086", "chat") is False


@pytest.mark.asyncio
async def test_all_ban_and_partial_unban_update_cache_immediately() -> None:
    repository = _Repository()
    service = UserBanService(repository, cache_ttl_seconds=60)  # type: ignore[arg-type]

    banned = await service.ban_user(
        user_id="10086",
        target_scope="all",
        operator_id="42",
        reason="刷屏",
    )
    unbanned = await service.unban_user(
        user_id="10086",
        target_scope="chat",
    )

    assert banned.changed is True
    assert banned.mutation_kind == "created"
    assert banned.status.active_scopes == {"chat", "command"}
    assert banned.affected_records[0].reason == "刷屏"
    assert unbanned.status.active_scopes == {"command"}
    assert unbanned.affected_records[0].ban_scope == "chat"
    assert await service.is_user_banned("10086", "chat") is False
    assert await service.is_user_banned("10086", "command") is True


@pytest.mark.asyncio
async def test_repeated_ban_overwrites_expiry_and_reason() -> None:
    repository = _Repository((_record("10086", "chat"),))
    service = UserBanService(repository, cache_ttl_seconds=60)  # type: ignore[arg-type]
    expires_at = datetime.now(UTC) + timedelta(days=7)

    result = await service.ban_user(
        user_id="10086",
        target_scope="chat",
        operator_id="42",
        expires_at=expires_at,
        reason="广告",
    )

    assert result.mutation_kind == "updated"
    assert result.affected_records[0].expires_at == expires_at
    assert result.affected_records[0].reason == "广告"


@pytest.mark.asyncio
async def test_expire_due_bans_deletes_records_and_updates_cache() -> None:
    expired = _record(
        "10086",
        "chat",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    active = _record("10086", "command")
    repository = _Repository((expired, active))
    service = UserBanService(repository, cache_ttl_seconds=60)  # type: ignore[arg-type]
    service._cache = service._build_cache((expired, active))

    removed = await service.expire_due_bans()

    assert removed == (expired,)
    assert service._cache["10086"].active_scopes == {"command"}


@pytest.mark.asyncio
async def test_refresh_failure_is_fail_closed_signal() -> None:
    repository = _Repository()
    repository.load_error = RuntimeError("数据库离线")
    service = UserBanService(repository, cache_ttl_seconds=0)  # type: ignore[arg-type]

    with pytest.raises(BanServiceUnavailableError, match="数据库离线"):
        await service.is_user_banned("10086", "chat")


@pytest.mark.asyncio
async def test_close_clears_cache_and_repository() -> None:
    repository = _Repository((_record("10086", "chat"),))
    service = UserBanService(repository, cache_ttl_seconds=60)  # type: ignore[arg-type]
    await service.initialize()

    await service.close()

    assert repository.closed is True
    assert service._cache == {}
    assert service._cache_revision is None
    assert service._refreshed_at is None
