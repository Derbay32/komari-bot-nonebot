"""用户封禁服务与运行时缓存。"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

from .models import (
    MAX_BAN_DURATION,
    BanListPage,
    BanMutationResult,
    BanRecord,
    BanScope,
    BanTargetScope,
    UserBanStatus,
    expand_target_scope,
    normalize_ban_reason,
)
from .repository import UserBanRepository

CACHE_TTL_SECONDS = 5.0


class BanServiceUnavailableError(RuntimeError):
    """封禁存储当前不可用。"""


class UserBanService:
    """封禁仓储、快照缓存和业务操作的统一入口。"""

    def __init__(
        self,
        repository: UserBanRepository | None = None,
        *,
        cache_ttl_seconds: float = CACHE_TTL_SECONDS,
    ) -> None:
        self.repository = repository or UserBanRepository()
        self.cache_ttl_seconds = cache_ttl_seconds
        self._cache: dict[str, UserBanStatus] = {}
        self._refreshed_at: float | None = None
        self._refresh_lock = asyncio.Lock()
        self._mutation_lock = asyncio.Lock()

    @staticmethod
    def _build_cache(records: tuple[BanRecord, ...]) -> dict[str, UserBanStatus]:
        grouped: dict[str, list[BanRecord]] = {}
        for record in records:
            grouped.setdefault(record.user_id, []).append(record)
        return {
            user_id: UserBanStatus(user_id=user_id, records=tuple(user_records))
            for user_id, user_records in grouped.items()
        }

    def _cache_is_fresh(self, now: float) -> bool:
        return (
            self._refreshed_at is not None
            and now - self._refreshed_at < self.cache_ttl_seconds
        )

    @staticmethod
    def _unavailable(action: str, error: Exception) -> BanServiceUnavailableError:
        return BanServiceUnavailableError(f"用户封禁存储{action}失败：{error}")

    @staticmethod
    def _active_status(status: UserBanStatus) -> UserBanStatus:
        return UserBanStatus(user_id=status.user_id, records=status.active_records)

    @staticmethod
    def _validate_expires_at(expires_at: datetime | None) -> None:
        if expires_at is None:
            return
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            msg = "封禁到期时间必须包含时区"
            raise ValueError(msg)
        now = datetime.now(UTC)
        if expires_at <= now:
            msg = "封禁到期时间必须晚于当前时间"
            raise ValueError(msg)
        if expires_at > now + MAX_BAN_DURATION:
            msg = "封禁时长不能超过十年"
            raise ValueError(msg)

    async def refresh(self, *, force: bool = False) -> None:
        """按需刷新全部有效封禁快照。"""
        now = time.monotonic()
        if not force and self._cache_is_fresh(now):
            return

        async with self._refresh_lock:
            now = time.monotonic()
            if not force and self._cache_is_fresh(now):
                return
            try:
                await self.repository.initialize()
                records = await self.repository.load_all()
            except Exception as error:
                raise self._unavailable("刷新", error) from error
            self._cache = self._build_cache(records)
            self._refreshed_at = time.monotonic()

    async def initialize(self) -> None:
        """初始化仓储并建立首份快照。"""
        await self.refresh(force=True)

    async def close(self) -> None:
        """清理缓存和数据库资源。"""
        await self.repository.close()
        self._cache.clear()
        self._refreshed_at = None

    async def is_user_banned(self, user_id: str, scope: BanScope) -> bool:
        """检查用户是否被封禁指定作用域。"""
        await self.refresh()
        status = self._cache.get(user_id)
        if status is None:
            return False
        active_status = self._active_status(status)
        self._replace_cached_status(active_status)
        return any(record.ban_scope == scope for record in active_status.records)

    async def get_status(self, user_id: str) -> UserBanStatus:
        """读取用户当前有效的封禁状态。"""
        await self.refresh()
        status = self._active_status(
            self._cache.get(user_id, UserBanStatus(user_id=user_id))
        )
        self._replace_cached_status(status)
        return status

    def _replace_cached_status(self, status: UserBanStatus) -> None:
        if status.records:
            self._cache[status.user_id] = status
            return
        self._cache.pop(status.user_id, None)

    async def ban_user(
        self,
        *,
        user_id: str,
        target_scope: BanTargetScope,
        operator_id: str,
        expires_at: datetime | None = None,
        reason: str | None = None,
    ) -> BanMutationResult:
        """持久化新增或覆盖封禁，并立即更新本进程缓存。"""
        normalized_reason = normalize_ban_reason(reason)
        self._validate_expires_at(expires_at)
        async with self._mutation_lock:
            await self.refresh()
            try:
                mutation_kind, affected, records = await self.repository.add_scopes(
                    user_id=user_id,
                    scopes=expand_target_scope(target_scope),
                    operator_id=operator_id,
                    reason=normalized_reason,
                    expires_at=expires_at,
                )
            except Exception as error:
                raise self._unavailable("写入", error) from error
            status = UserBanStatus(user_id=user_id, records=records)
            self._replace_cached_status(status)
            return BanMutationResult(
                status=status,
                target_scope=target_scope,
                changed=mutation_kind != "unchanged",
                mutation_kind=mutation_kind,
                affected_records=affected,
            )

    async def unban_user(
        self,
        *,
        user_id: str,
        target_scope: BanTargetScope,
    ) -> BanMutationResult:
        """持久化解封并立即更新本进程缓存。"""
        async with self._mutation_lock:
            await self.refresh()
            try:
                removed, records = await self.repository.remove_scopes(
                    user_id=user_id,
                    scopes=expand_target_scope(target_scope),
                )
            except Exception as error:
                raise self._unavailable("删除", error) from error
            status = UserBanStatus(user_id=user_id, records=records)
            self._replace_cached_status(status)
            return BanMutationResult(
                status=status,
                target_scope=target_scope,
                changed=bool(removed),
                mutation_kind="removed" if removed else "unchanged",
                affected_records=removed,
            )

    async def expire_due_bans(self) -> tuple[BanRecord, ...]:
        """删除已到期封禁，并同步移除本进程缓存。"""
        async with self._mutation_lock:
            try:
                await self.repository.initialize()
                expired = await self.repository.delete_expired()
            except Exception as error:
                raise self._unavailable("清理到期记录", error) from error

            for record in expired:
                cached = self._cache.get(record.user_id)
                if cached is None:
                    continue
                remaining = tuple(
                    item
                    for item in cached.records
                    if item.ban_scope != record.ban_scope
                )
                self._replace_cached_status(
                    UserBanStatus(user_id=record.user_id, records=remaining)
                )
            return expired

    async def list_bans(
        self,
        *,
        scope: BanScope | None,
        limit: int,
        offset: int,
    ) -> BanListPage:
        """分页列出当前有效的封禁用户。"""
        await self.refresh()
        try:
            items, total = await self.repository.list_statuses(
                scope=scope,
                limit=limit,
                offset=offset,
            )
        except Exception as error:
            raise self._unavailable("查询", error) from error
        active_items = tuple(self._active_status(status) for status in items)
        return BanListPage(
            items=active_items,
            total=total,
            limit=limit,
            offset=offset,
        )


_service = UserBanService()


def get_service() -> UserBanService:
    """获取全局用户封禁服务。"""
    return _service
