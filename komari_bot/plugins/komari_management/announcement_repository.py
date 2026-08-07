"""维护公告请求的跨 worker 幂等与冷却账本（SQLModel + nonebot-plugin-orm AsyncSession）。

连接池与 engine 生命周期由 nonebot-plugin-orm 托管（本模块不再依赖
自研 asyncpg 池）；表结构由 Alembic 迁移统一管理，
启动期与懒路径均无任何 DDL。

跨 worker 串行化依赖 PostgreSQL 事务级咨询锁 ``pg_advisory_xact_lock``：
同一 request_id 的并发抢占在锁内先到先得，冷却与幂等判定共享同一事务快照。
间隔运算使用 ``CAST(:param AS INTERVAL)``（``_interval`` 辅助）。

SQLModel 字段在 Pyright 下被推断为 Python 值类型而非列表达式，因此列访问
统一走 ``模型.__table__.c``（与 user_ban / komari_decision 仓储同一约定）。
JSONB 列写 SQL NULL 必须使用 ``sqlalchemy.null()``。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import (
    Double,
    Interval,
    delete,
    func,
    select,
    text,
    update,
)
from sqlalchemy import cast as sqlalchemy_cast
from sqlalchemy.dialects.postgresql import insert as postgres_insert

from .orm_models import AnnouncementDispatchRow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

type DispatchClaimState = Literal[
    "claimed",
    "replay",
    "in_progress",
    "payload_conflict",
    "cooldown",
    "reconciliation_required",
]

# 表结构由 Alembic 迁移统一管理，运行时不执行 DDL。
_ADVISORY_LOCK_ID = 6_126_613_117_029_977_126

_D = AnnouncementDispatchRow.__table__


def _interval(seconds: float) -> Any:
    """把秒数构造成 ``CAST(%s AS INTERVAL)`` 表达式（PG 侧原生 interval）。"""
    return sqlalchemy_cast(timedelta(seconds=seconds), Interval)


def _open_session() -> "AsyncSession":
    """打开绑定 nonebot-plugin-orm 共享引擎的会话。"""
    from nonebot_plugin_orm import get_session

    return get_session(expire_on_commit=False)


@dataclass(frozen=True, slots=True)
class DispatchClaim:
    """一次公告请求账本抢占结果。"""

    state: DispatchClaimState
    response_payload: dict[str, Any] | None = None
    remaining_seconds: float | None = None


class AnnouncementDispatchRepository:
    """PostgreSQL 公告幂等账本。"""

    def __init__(self) -> None:
        self._initialize_lock = asyncio.Lock()
        self._ready = False

    async def initialize(self) -> None:
        """单飞确认 ORM 存储可连接；表结构由 Alembic 迁移统一管理。"""
        async with self._initialize_lock:
            if self._ready:
                return
            session = _open_session()
            try:
                await session.execute(select(1))
            finally:
                await session.close()
            self._ready = True

    async def close(self) -> None:
        """重置就绪状态；engine 生命周期由 nonebot-plugin-orm 托管。"""
        async with self._initialize_lock:
            self._ready = False

    def _require_ready(self) -> None:
        if not self._ready:
            message = "公告幂等账本尚未初始化"
            raise RuntimeError(message)

    @staticmethod
    def _decode_response(value: object) -> dict[str, Any] | None:
        if value is None:
            return None
        payload = json.loads(value) if isinstance(value, str) else value
        return payload if isinstance(payload, dict) else None

    async def claim(  # noqa: PLR0911 - 状态机的每个结果都需要明确返回
        self,
        *,
        request_id: str,
        payload_hash: str,
        owner_token: str,
        lease_seconds: int,
        cooldown_seconds: float,
    ) -> DispatchClaim:
        """原子校验 request ID、全局冷却并抢占新公告请求。"""
        await self.initialize()
        session = _open_session()
        try:
            async with session.begin():
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(:lock_id)"),
                    {"lock_id": _ADVISORY_LOCK_ID},
                )
                await session.execute(
                    delete(_D).where(
                        _D.c.created_at
                        < func.now() - _interval(30 * 24 * 3600)
                    )
                )
                existing = (
                    await session.execute(
                        select(
                            _D.c.payload_hash,
                            _D.c.status,
                            _D.c.response_payload,
                            _D.c.lease_expires_at,
                        )
                        .where(_D.c.request_id == request_id)
                        .with_for_update()
                    )
                ).one_or_none()
                if existing is not None:
                    if str(existing.payload_hash) != payload_hash:
                        return DispatchClaim(state="payload_conflict")
                    status = str(existing.status)
                    if status == "completed":
                        response = self._decode_response(
                            existing.response_payload
                        )
                        if response is None:
                            return DispatchClaim(
                                state="reconciliation_required"
                            )
                        return DispatchClaim(
                            state="replay", response_payload=response
                        )
                    if status == "reconciliation_required":
                        return DispatchClaim(
                            state="reconciliation_required"
                        )

                    expired = (
                        await session.execute(
                            select(_D.c.lease_expires_at <= func.now()).where(
                                _D.c.request_id == request_id
                            )
                        )
                    ).scalar_one()
                    if not bool(expired):
                        return DispatchClaim(state="in_progress")
                    await session.execute(
                        update(_D)
                        .where(_D.c.request_id == request_id)
                        .values(
                            status="reconciliation_required",
                            owner_token=None,
                            lease_expires_at=None,
                            updated_at=func.now(),
                        )
                    )
                    return DispatchClaim(state="reconciliation_required")

                if cooldown_seconds > 0:
                    remaining = (
                        await session.execute(
                            select(
                                func.greatest(
                                    sqlalchemy_cast(
                                        float(cooldown_seconds), Double
                                    )
                                    - func.extract(
                                        "epoch",
                                        func.now() - _D.c.created_at,
                                    ),
                                    0,
                                )
                            )
                            .order_by(_D.c.created_at.desc())
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if remaining is not None and float(remaining) > 0:
                        return DispatchClaim(
                            state="cooldown",
                            remaining_seconds=float(remaining),
                        )

                await session.execute(
                    postgres_insert(_D)
                    .values(
                        request_id=request_id,
                        payload_hash=payload_hash,
                        status="processing",
                        owner_token=owner_token,
                        lease_expires_at=func.now() + _interval(lease_seconds),
                    )
                )
        finally:
            await session.close()
        return DispatchClaim(state="claimed")

    async def complete(
        self,
        *,
        request_id: str,
        owner_token: str,
        response_payload: dict[str, Any],
    ) -> bool:
        """仅由当前 owner 持久化最终响应并完成请求。"""
        self._require_ready()
        session = _open_session()
        try:
            async with session.begin():
                completed = (
                    await session.execute(
                        update(_D)
                        .where(
                            _D.c.request_id == request_id,
                            _D.c.status == "processing",
                            _D.c.owner_token == owner_token,
                            _D.c.lease_expires_at > func.now(),
                        )
                        .values(
                            status="completed",
                            response_payload=response_payload,
                            owner_token=None,
                            lease_expires_at=None,
                            completed_at=func.now(),
                            updated_at=func.now(),
                        )
                        .returning(_D.c.request_id)
                    )
                ).scalar_one_or_none()
        finally:
            await session.close()
        return completed is not None

    async def mark_reconciliation_required(
        self,
        *,
        request_id: str,
        owner_token: str,
    ) -> None:
        """异常退出时阻止该 request ID 被自动重发。"""
        self._require_ready()
        session = _open_session()
        try:
            async with session.begin():
                await session.execute(
                    update(_D)
                    .where(
                        _D.c.request_id == request_id,
                        _D.c.status == "processing",
                        _D.c.owner_token == owner_token,
                    )
                    .values(
                        status="reconciliation_required",
                        owner_token=None,
                        lease_expires_at=None,
                        updated_at=func.now(),
                    )
                )
        finally:
            await session.close()

    async def cancel_unstarted(
        self,
        *,
        request_id: str,
        owner_token: str,
    ) -> bool:
        """在确认尚未调用平台发送接口时删除当前抢占。"""
        self._require_ready()
        session = _open_session()
        try:
            async with session.begin():
                deleted = (
                    await session.execute(
                        delete(_D)
                        .where(
                            _D.c.request_id == request_id,
                            _D.c.status == "processing",
                            _D.c.owner_token == owner_token,
                        )
                        .returning(_D.c.request_id)
                    )
                ).scalar_one_or_none()
        finally:
            await session.close()
        return deleted is not None


@dataclass(slots=True)
class _MemoryDispatch:
    payload_hash: str
    status: Literal["processing", "completed", "reconciliation_required"]
    owner_token: str | None
    lease_expires_at: float | None
    response_payload: dict[str, Any] | None
    created_at: float


class InMemoryAnnouncementDispatchRepository:
    """路由单元测试使用的同契约内存账本。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._records: dict[str, _MemoryDispatch] = {}

    async def claim(  # noqa: PLR0911 - 状态机的每个结果都需要明确返回
        self,
        *,
        request_id: str,
        payload_hash: str,
        owner_token: str,
        lease_seconds: int,
        cooldown_seconds: float,
    ) -> DispatchClaim:
        async with self._lock:
            now = time.monotonic()
            existing = self._records.get(request_id)
            if existing is not None:
                if existing.payload_hash != payload_hash:
                    return DispatchClaim(state="payload_conflict")
                if existing.status == "completed":
                    return DispatchClaim(
                        state="replay",
                        response_payload=existing.response_payload,
                    )
                if existing.status == "reconciliation_required":
                    return DispatchClaim(state="reconciliation_required")
                if existing.lease_expires_at is not None and existing.lease_expires_at > now:
                    return DispatchClaim(state="in_progress")
                existing.status = "reconciliation_required"
                existing.owner_token = None
                existing.lease_expires_at = None
                return DispatchClaim(state="reconciliation_required")

            if self._records and cooldown_seconds > 0:
                latest = max(item.created_at for item in self._records.values())
                remaining = cooldown_seconds - (now - latest)
                if remaining > 0:
                    return DispatchClaim(
                        state="cooldown",
                        remaining_seconds=remaining,
                    )
            self._records[request_id] = _MemoryDispatch(
                payload_hash=payload_hash,
                status="processing",
                owner_token=owner_token,
                lease_expires_at=now + lease_seconds,
                response_payload=None,
                created_at=now,
            )
            return DispatchClaim(state="claimed")

    async def complete(
        self,
        *,
        request_id: str,
        owner_token: str,
        response_payload: dict[str, Any],
    ) -> bool:
        async with self._lock:
            record = self._records.get(request_id)
            if (
                record is None
                or record.status != "processing"
                or record.owner_token != owner_token
                or record.lease_expires_at is None
                or record.lease_expires_at <= time.monotonic()
            ):
                return False
            record.status = "completed"
            record.owner_token = None
            record.lease_expires_at = None
            record.response_payload = dict(response_payload)
            return True

    async def mark_reconciliation_required(
        self,
        *,
        request_id: str,
        owner_token: str,
    ) -> None:
        async with self._lock:
            record = self._records.get(request_id)
            if (
                record is not None
                and record.status == "processing"
                and record.owner_token == owner_token
            ):
                record.status = "reconciliation_required"
                record.owner_token = None
                record.lease_expires_at = None

    async def cancel_unstarted(
        self,
        *,
        request_id: str,
        owner_token: str,
    ) -> bool:
        async with self._lock:
            record = self._records.get(request_id)
            if (
                record is None
                or record.status != "processing"
                or record.owner_token != owner_token
            ):
                return False
            del self._records[request_id]
            return True


_repository = AnnouncementDispatchRepository()


def get_announcement_dispatch_repository() -> AnnouncementDispatchRepository:
    return _repository


async def close_announcement_dispatch_repository() -> None:
    await _repository.close()
