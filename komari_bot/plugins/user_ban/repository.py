"""用户封禁 PostgreSQL 访问层（SQLModel + nonebot-plugin-orm AsyncSession）。

连接池与 engine 生命周期由 nonebot-plugin-orm 托管（本模块不再依赖
自研 asyncpg 池）；表结构由 Alembic 迁移统一管理，
启动期与懒路径均无任何 DDL。

REPEATABLE READ 只读快照通过 ``session.execute(..., execution_options=...)``
实现：SQLAlchemy 在事务开始前把 ``isolation_level`` /
``postgresql_readonly`` 应用到连接（``SessionTransaction._connection_for_bind``
先 ``conn.execution_options(**execution_options)`` 再 ``conn.begin()``），
因此同一事务内 revision 水位与全部有效记录来自同一数据库快照。

SQLModel 字段在 Pyright 下被推断为 Python 值类型而非列表达式，因此
列访问统一走 ``模型.__table__.c``（与 typed_config 存储层同一约定）。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from sqlalchemy import (
    ColumnElement,
    and_,
    delete,
    func,
    null,
    or_,
    select,
    text,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import insert as postgres_insert

from .models import (
    BanMutationKind,
    BanRecord,
    BanScope,
    ExpiredBanNotification,
    UserBanStatus,
)
from .orm_models import (
    UserBanCacheState,
    UserBanNotificationOutbox,
    UserBanRow,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.engine import CursorResult
    from sqlalchemy.ext.asyncio import AsyncSession

_BANS = UserBanRow.__table__
_STATE = UserBanCacheState.__table__
_OUTBOX = UserBanNotificationOutbox.__table__


def _active_condition() -> ColumnElement[bool]:
    """有效记录谓词：永久封禁或未到期。"""
    return or_(
        _BANS.c.expires_at.is_(None),
        _BANS.c.expires_at > func.now(),
    )


def _open_session() -> "AsyncSession":
    """打开绑定 nonebot-plugin-orm 共享引擎的会话。"""
    from nonebot_plugin_orm import get_session

    return get_session(expire_on_commit=False)


@dataclass(frozen=True, slots=True)
class BanCacheSnapshot:
    """与单个数据库快照一致的缓存版本和有效封禁记录。"""

    revision: int
    records: tuple[BanRecord, ...]


class UserBanRepository:
    """用户封禁数据仓储。"""

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

    @staticmethod
    def _order_by_scopes(
        rows: Sequence[UserBanRow],
        scopes: tuple[BanScope, ...],
    ) -> list[UserBanRow]:
        """按请求作用域顺序稳定重排（INSERT/DELETE RETURNING 无顺序保证）。"""
        by_scope = {row.ban_scope: row for row in rows}
        return [by_scope[scope] for scope in scopes if scope in by_scope]

    @staticmethod
    def _model_to_record(entity: UserBanRow) -> BanRecord:
        return BanRecord(
            user_id=entity.user_id,
            ban_scope=cast("BanScope", entity.ban_scope),
            operator_id=entity.operator_id,
            reason=entity.reason,
            expires_at=entity.expires_at,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )

    @staticmethod
    def _records_payload(records: tuple[BanRecord, ...]) -> list[dict[str, Any]]:
        """构造写入 outbox JSONB 列的 Python 结构（无需 JSON 字符串化）。"""
        return [
            {
                "user_id": record.user_id,
                "ban_scope": record.ban_scope,
                "operator_id": record.operator_id,
                "reason": record.reason,
                "expires_at": (
                    record.expires_at.isoformat()
                    if record.expires_at is not None
                    else None
                ),
                "created_at": record.created_at.isoformat(),
                "updated_at": record.updated_at.isoformat(),
            }
            for record in records
        ]

    @classmethod
    def _serialize_records(cls, records: tuple[BanRecord, ...]) -> str:
        return json.dumps(
            cls._records_payload(records),
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _parse_outbox_datetime(value: object) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value))

    @classmethod
    def _outbox_row_to_notification(cls, row: Any) -> ExpiredBanNotification:
        raw_records = row.records
        payload = json.loads(raw_records) if isinstance(raw_records, str) else raw_records
        if not isinstance(payload, list):
            message = "自然解封通知 outbox 的 records 结构无效"
            raise TypeError(message)
        records: list[BanRecord] = []
        for item in payload:
            if not isinstance(item, dict):
                message = "自然解封通知 outbox 包含无效记录"
                raise TypeError(message)
            created_at = cls._parse_outbox_datetime(item.get("created_at"))
            updated_at = cls._parse_outbox_datetime(item.get("updated_at"))
            if created_at is None or updated_at is None:
                message = "自然解封通知 outbox 缺少记录时间"
                raise ValueError(message)
            records.append(
                BanRecord(
                    user_id=str(item["user_id"]),
                    ban_scope=cast("BanScope", str(item["ban_scope"])),
                    operator_id=str(item["operator_id"]),
                    reason=(
                        str(item["reason"])
                        if item.get("reason") is not None
                        else None
                    ),
                    expires_at=cls._parse_outbox_datetime(item.get("expires_at")),
                    created_at=created_at,
                    updated_at=updated_at,
                )
            )
        return ExpiredBanNotification(
            notification_id=str(row.notification_id),
            user_id=str(row.user_id),
            records=tuple(records),
            attempt_count=int(row.attempt_count),
        )

    @classmethod
    def _rows_to_statuses(cls, rows: list[Any]) -> tuple[UserBanStatus, ...]:
        records_by_user: dict[str, list[BanRecord]] = {}
        user_order: list[str] = []
        for row in rows:
            record = cls._model_to_record(row[0])
            if record.user_id not in records_by_user:
                records_by_user[record.user_id] = []
                user_order.append(record.user_id)
            records_by_user[record.user_id].append(record)

        return tuple(
            UserBanStatus(user_id=user_id, records=tuple(records_by_user[user_id]))
            for user_id in user_order
        )

    async def get_cache_revision(self) -> int:
        """读取轻量缓存版本水位，不传输封禁明细。"""
        session = _open_session()
        try:
            state = await session.get(UserBanCacheState, 1)
        finally:
            await session.close()
        if state is None:
            msg = "user_ban 缓存版本记录不存在"
            raise RuntimeError(msg)
        return int(state.revision)

    async def load_snapshot(self) -> BanCacheSnapshot:
        """在可重复读只读事务中读取缓存版本与全部有效记录。

        首次语句通过 ``execution_options`` 携带
        ``isolation_level="REPEATABLE READ"`` 与 ``postgresql_readonly=True``，
        在事务开始前应用到连接，保证 revision 水位与记录集来自同一快照。
        """
        session = _open_session()
        try:
            async with session.begin():
                state = (
                    await session.execute(
                        select(UserBanCacheState).where(
                            _STATE.c.singleton_id == 1
                        ),
                        execution_options={
                            "isolation_level": "REPEATABLE READ",
                            "postgresql_readonly": True,
                        },
                    )
                ).scalar_one_or_none()
                if state is None:
                    msg = "user_ban 缓存版本记录不存在"
                    raise RuntimeError(msg)
                rows = (
                    await session.execute(
                        select(UserBanRow)
                        .where(_active_condition())
                        .order_by(_BANS.c.user_id, _BANS.c.ban_scope)
                    )
                ).scalars().all()
        finally:
            await session.close()
        return BanCacheSnapshot(
            revision=int(state.revision),
            records=tuple(self._model_to_record(row) for row in rows),
        )

    async def load_all(self) -> tuple[BanRecord, ...]:
        """兼容旧调用：读取一致快照中的全部有效记录。"""
        return (await self.load_snapshot()).records

    @staticmethod
    async def _bump_cache_revision(session: "AsyncSession") -> None:
        """在业务写事务中推进跨 worker 缓存版本。"""
        result = cast(
            "CursorResult[Any]",
            await session.execute(
                update(_STATE)
                .where(_STATE.c.singleton_id == 1)
                .values(
                    revision=_STATE.c.revision + 1,
                    updated_at=func.now(),
                )
            ),
        )
        if result.rowcount != 1:
            msg = "user_ban 缓存版本推进失败"
            raise RuntimeError(msg)

    async def add_scopes(
        self,
        *,
        user_id: str,
        scopes: tuple[BanScope, ...],
        operator_id: str,
        reason: str | None,
        expires_at: datetime | None,
    ) -> tuple[
        BanMutationKind,
        tuple[BanRecord, ...],
        tuple[BanRecord, ...],
    ]:
        """原子新增或覆盖一个或多个封禁作用域。"""
        session = _open_session()
        try:
            async with session.begin():
                existing = (
                    await session.execute(
                        select(UserBanRow)
                        .where(
                            _BANS.c.user_id == user_id,
                            _BANS.c.ban_scope.in_(list(scopes)),
                        )
                        .order_by(_BANS.c.ban_scope)
                    )
                ).scalars().all()

                statement = postgres_insert(UserBanRow).values(
                    [
                        {
                            "user_id": user_id,
                            "ban_scope": scope,
                            "operator_id": operator_id,
                            "reason": reason,
                            "expires_at": expires_at,
                        }
                        for scope in scopes
                    ]
                )
                excluded = statement.excluded
                changed_rows = (
                    await session.execute(
                        statement.on_conflict_do_update(
                            index_elements=["user_id", "ban_scope"],
                            set_={
                                "operator_id": excluded.operator_id,
                                "reason": excluded.reason,
                                "expires_at": excluded.expires_at,
                                "updated_at": func.now(),
                            },
                            where=tuple_(
                                _BANS.c.operator_id,
                                _BANS.c.reason,
                                _BANS.c.expires_at,
                            ).is_distinct_from(
                                tuple_(
                                    excluded.operator_id,
                                    excluded.reason,
                                    excluded.expires_at,
                                )
                            ),
                        )
                        .returning(UserBanRow),
                        # 同事务内 existing 查询已载入同主键实体，
                        # 必须强制用 RETURNING 结果刷新，否则返回旧值
                        execution_options={"populate_existing": True},
                    )
                ).scalars().all()
                changed_rows = self._order_by_scopes(changed_rows, scopes)

                current_rows = (
                    await session.execute(
                        select(UserBanRow)
                        .where(
                            _BANS.c.user_id == user_id,
                            _active_condition(),
                        )
                        .order_by(_BANS.c.ban_scope)
                    )
                ).scalars().all()
                if changed_rows:
                    await self._bump_cache_revision(session)
        finally:
            await session.close()

        if not changed_rows:
            mutation_kind: BanMutationKind = "unchanged"
        elif existing:
            mutation_kind = "updated"
        else:
            mutation_kind = "created"
        affected = tuple(self._model_to_record(row) for row in changed_rows)
        current = tuple(self._model_to_record(row) for row in current_rows)
        return mutation_kind, affected, current

    async def remove_scopes(
        self,
        *,
        user_id: str,
        scopes: tuple[BanScope, ...],
    ) -> tuple[tuple[BanRecord, ...], tuple[BanRecord, ...]]:
        """原子删除一个或多个封禁作用域，并返回删除前内容。"""
        session = _open_session()
        try:
            async with session.begin():
                deleted_rows = (
                    await session.execute(
                        delete(UserBanRow)
                        .where(
                            _BANS.c.user_id == user_id,
                            _BANS.c.ban_scope.in_(list(scopes)),
                            _active_condition(),
                        )
                        .returning(UserBanRow)
                    )
                ).scalars().all()
                deleted_rows = self._order_by_scopes(deleted_rows, scopes)
                current_rows = (
                    await session.execute(
                        select(UserBanRow)
                        .where(
                            _BANS.c.user_id == user_id,
                            _active_condition(),
                        )
                        .order_by(_BANS.c.ban_scope)
                    )
                ).scalars().all()
                if deleted_rows:
                    await self._bump_cache_revision(session)
        finally:
            await session.close()
        deleted = tuple(self._model_to_record(row) for row in deleted_rows)
        current = tuple(self._model_to_record(row) for row in current_rows)
        return deleted, current

    async def delete_expired(self) -> tuple[BanRecord, ...]:
        """原子删除到期记录，并在同一事务写入自然解封通知 outbox。"""
        session = _open_session()
        try:
            async with session.begin():
                deleted_rows = (
                    await session.execute(
                        delete(UserBanRow)
                        .where(
                            _BANS.c.expires_at.is_not(None),
                            _BANS.c.expires_at <= func.now(),
                        )
                        .returning(UserBanRow)
                    )
                ).scalars().all()
                records = [
                    self._model_to_record(row) for row in deleted_rows
                ]
                records.sort(key=lambda record: (record.user_id, record.ban_scope))
                records_by_user: dict[str, list[BanRecord]] = {}
                for record in records:
                    records_by_user.setdefault(record.user_id, []).append(record)
                for user_id, user_records in records_by_user.items():
                    session.add(
                        UserBanNotificationOutbox(
                            notification_id=uuid4().hex,
                            user_id=user_id,
                            notification_kind="natural_expiry",
                            records=self._records_payload(tuple(user_records)),
                        )
                    )
                if records:
                    await self._bump_cache_revision(session)
        finally:
            await session.close()
        return tuple(records)

    async def claim_expired_notification(
        self,
        *,
        owner_token: str,
        lease_seconds: int,
    ) -> ExpiredBanNotification | None:
        """使用 SKIP LOCKED 领取一条待发送自然解封通知。"""
        normalized_owner = owner_token.strip()
        if not normalized_owner:
            message = "自然解封通知 owner_token 不能为空"
            raise ValueError(message)
        if not 10 <= lease_seconds <= 3600:
            message = "自然解封通知租约必须在 10 到 3600 秒之间"
            raise ValueError(message)
        session = _open_session()
        try:
            async with session.begin():
                candidate = (
                    select(_OUTBOX.c.notification_id)
                    .where(
                        _OUTBOX.c.notification_kind == "natural_expiry",
                        _OUTBOX.c.records.is_not(None),
                        _OUTBOX.c.available_at <= func.now(),
                        or_(
                            _OUTBOX.c.status == "pending",
                            and_(
                                _OUTBOX.c.status == "processing",
                                _OUTBOX.c.lease_expires_at <= func.now(),
                            ),
                        ),
                    )
                    .order_by(
                        _OUTBOX.c.available_at,
                        _OUTBOX.c.created_at,
                        _OUTBOX.c.notification_id,
                    )
                    .with_for_update(skip_locked=True)
                    .limit(1)
                    .cte("candidate")
                )
                row = (
                    await session.execute(
                        update(_OUTBOX)
                        .where(_OUTBOX.c.notification_id == candidate.c.notification_id)
                        .values(
                            status="processing",
                            owner_token=normalized_owner,
                            lease_expires_at=func.now()
                            + timedelta(seconds=lease_seconds),
                            attempt_count=_OUTBOX.c.attempt_count + 1,
                            updated_at=func.now(),
                        )
                        .returning(
                            _OUTBOX.c.notification_id,
                            _OUTBOX.c.user_id,
                            _OUTBOX.c.records,
                            _OUTBOX.c.attempt_count,
                        )
                    )
                ).one_or_none()
        finally:
            await session.close()
        if row is None:
            return None
        return self._outbox_row_to_notification(row)

    async def acknowledge_expired_notification(
        self,
        *,
        notification_id: str,
        owner_token: str,
    ) -> bool:
        """确认发送完成，并立即清除包含封禁理由的 outbox payload。"""
        session = _open_session()
        try:
            async with session.begin():
                acknowledged = (
                    await session.execute(
                        update(_OUTBOX)
                        .where(
                            _OUTBOX.c.notification_id == notification_id,
                            _OUTBOX.c.status == "processing",
                            _OUTBOX.c.owner_token == owner_token,
                            _OUTBOX.c.lease_expires_at > func.now(),
                        )
                        .values(
                            status="sent",
                            # JSONB 列写 None 会变成 JSON null 而非 SQL NULL，
                            # 必须用 sqlalchemy.null() 保持旧实现的清除语义
                            records=null(),
                            owner_token=None,
                            lease_expires_at=None,
                            last_error_code=None,
                            sent_at=func.now(),
                            updated_at=func.now(),
                        )
                        .returning(_OUTBOX.c.notification_id)
                    )
                ).scalar_one_or_none()
        finally:
            await session.close()
        return acknowledged is not None

    async def retry_expired_notification(
        self,
        *,
        notification_id: str,
        owner_token: str,
        error_code: str,
        retry_delay_seconds: float,
    ) -> bool:
        """发送失败后按稳定错误码重新排队，保留原 payload。"""
        if not 0 <= retry_delay_seconds <= 86400:
            message = "自然解封通知重试延迟必须在 0 到 86400 秒之间"
            raise ValueError(message)
        session = _open_session()
        try:
            async with session.begin():
                retried = (
                    await session.execute(
                        update(_OUTBOX)
                        .where(
                            _OUTBOX.c.notification_id == notification_id,
                            _OUTBOX.c.status == "processing",
                            _OUTBOX.c.owner_token == owner_token,
                        )
                        .values(
                            status="pending",
                            owner_token=None,
                            lease_expires_at=None,
                            available_at=func.now()
                            + timedelta(seconds=retry_delay_seconds),
                            last_error_code=error_code,
                            updated_at=func.now(),
                        )
                        .returning(_OUTBOX.c.notification_id)
                    )
                ).scalar_one_or_none()
        finally:
            await session.close()
        return retried is not None

    async def list_statuses(
        self,
        *,
        scope: BanScope | None,
        limit: int,
        offset: int,
    ) -> tuple[tuple[UserBanStatus, ...], int]:
        """按用户分页列出当前有效的封禁状态。"""
        session = _open_session()
        try:
            conditions = [_active_condition()]
            if scope is not None:
                conditions.append(_BANS.c.ban_scope == scope)
            total = (
                await session.execute(
                    select(func.count(func.distinct(_BANS.c.user_id))).where(
                        *conditions
                    )
                )
            ).scalar_one()
            latest = (
                select(
                    _BANS.c.user_id,
                    func.max(_BANS.c.updated_at).label("latest_update"),
                )
                .where(*conditions)
                .group_by(_BANS.c.user_id)
                .order_by(text("latest_update DESC"), _BANS.c.user_id)
                .limit(limit)
                .offset(offset)
                .cte("matching_users")
            )
            rows = list(
                (
                    await session.execute(
                        select(UserBanRow, latest.c.latest_update)
                        .join(latest, latest.c.user_id == _BANS.c.user_id)
                        .where(_active_condition())
                        .order_by(
                            text("latest_update DESC"),
                            _BANS.c.user_id,
                            _BANS.c.ban_scope,
                        )
                    )
                ).all()
            )
        finally:
            await session.close()
        return self._rows_to_statuses(rows), int(total)
