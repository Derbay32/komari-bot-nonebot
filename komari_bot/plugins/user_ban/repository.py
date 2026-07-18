"""用户封禁 PostgreSQL 访问层。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from komari_bot.common.database_config import get_shared_database_config
from komari_bot.common.postgres import create_postgres_pool

from .models import (
    BanMutationKind,
    BanRecord,
    BanScope,
    ExpiredBanNotification,
    UserBanStatus,
)

if TYPE_CHECKING:
    import asyncpg


_RECORD_COLUMNS = """
user_id, ban_scope, operator_id, reason, expires_at, created_at, updated_at
"""
_ACTIVE_PREDICATE = "(expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)"


@dataclass(frozen=True, slots=True)
class BanCacheSnapshot:
    """与单个数据库快照一致的缓存版本和有效封禁记录。"""

    revision: int
    records: tuple[BanRecord, ...]


class UserBanRepository:
    """用户封禁数据仓储。"""

    def __init__(self) -> None:
        self._pool: asyncpg.Pool | None = None
        self._initialize_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """初始化连接池和表结构。"""
        if self._pool is not None:
            return

        async with self._initialize_lock:
            if self._pool is not None:
                return
            pool = await create_postgres_pool(
                get_shared_database_config(),
                command_timeout=30,
            )
            try:
                sql = Path(__file__).with_name("init_db.sql").read_text(
                    encoding="utf-8"
                )
                async with pool.acquire() as conn:
                    await conn.execute(sql)
            except Exception:
                await pool.close()
                raise
            self._pool = pool

    async def close(self) -> None:
        """关闭数据库连接池。"""
        async with self._initialize_lock:
            pool = self._pool
            self._pool = None
            if pool is not None:
                await pool.close()

    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            msg = "user_ban 数据库尚未初始化"
            raise RuntimeError(msg)
        return self._pool

    @staticmethod
    def _row_to_record(row: Any) -> BanRecord:
        return BanRecord(
            user_id=str(row["user_id"]),
            ban_scope=cast("BanScope", str(row["ban_scope"])),
            operator_id=str(row["operator_id"]),
            reason=str(row["reason"]) if row["reason"] is not None else None,
            expires_at=row["expires_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _serialize_records(records: tuple[BanRecord, ...]) -> str:
        return json.dumps(
            [
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
            ],
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
        raw_records = row["records"]
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
            notification_id=str(row["notification_id"]),
            user_id=str(row["user_id"]),
            records=tuple(records),
            attempt_count=int(row["attempt_count"]),
        )

    @classmethod
    def _rows_to_statuses(cls, rows: list[Any]) -> tuple[UserBanStatus, ...]:
        records_by_user: dict[str, list[BanRecord]] = {}
        user_order: list[str] = []
        for row in rows:
            record = cls._row_to_record(row)
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
        pool = self._require_pool()
        async with pool.acquire() as conn:
            revision = await conn.fetchval(
                """
                SELECT revision
                FROM komari_user_ban_cache_state
                WHERE singleton_id = 1
                """
            )
        if revision is None:
            msg = "user_ban 缓存版本记录不存在"
            raise RuntimeError(msg)
        return int(revision)

    async def load_snapshot(self) -> BanCacheSnapshot:
        """在可重复读事务中读取缓存版本与全部有效记录。"""
        pool = self._require_pool()
        async with pool.acquire() as conn, conn.transaction(
            isolation="repeatable_read",
            readonly=True,
        ):
            revision = await conn.fetchval(
                """
                SELECT revision
                FROM komari_user_ban_cache_state
                WHERE singleton_id = 1
                """
            )
            if revision is None:
                msg = "user_ban 缓存版本记录不存在"
                raise RuntimeError(msg)
            rows = await conn.fetch(
                f"""
                SELECT {_RECORD_COLUMNS}
                FROM komari_user_bans
                WHERE {_ACTIVE_PREDICATE}
                ORDER BY user_id, ban_scope
                """
            )
        return BanCacheSnapshot(
            revision=int(revision),
            records=tuple(self._row_to_record(row) for row in rows),
        )

    async def load_all(self) -> tuple[BanRecord, ...]:
        """兼容旧调用：读取一致快照中的全部有效记录。"""
        return (await self.load_snapshot()).records

    @staticmethod
    async def _bump_cache_revision(conn: Any) -> None:
        """在业务写事务中推进跨 worker 缓存版本。"""
        result = await conn.execute(
            """
            UPDATE komari_user_ban_cache_state
            SET revision = revision + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE singleton_id = 1
            """
        )
        if result != "UPDATE 1":
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
        pool = self._require_pool()
        async with pool.acquire() as conn, conn.transaction():
            existing = await conn.fetch(
                f"""
                SELECT {_RECORD_COLUMNS}
                FROM komari_user_bans
                WHERE user_id = $1 AND ban_scope = ANY($2::TEXT[])
                ORDER BY ban_scope
                """,
                user_id,
                list(scopes),
            )
            changed_rows = await conn.fetch(
                f"""
                INSERT INTO komari_user_bans (
                    user_id, ban_scope, operator_id, reason, expires_at
                )
                SELECT $1, scope, $2, $3, $4
                FROM UNNEST($5::TEXT[]) AS scope
                ON CONFLICT (user_id, ban_scope) DO UPDATE
                SET operator_id = EXCLUDED.operator_id,
                    reason = EXCLUDED.reason,
                    expires_at = EXCLUDED.expires_at,
                    updated_at = CURRENT_TIMESTAMP
                WHERE (
                    komari_user_bans.operator_id,
                    komari_user_bans.reason,
                    komari_user_bans.expires_at
                ) IS DISTINCT FROM (
                    EXCLUDED.operator_id,
                    EXCLUDED.reason,
                    EXCLUDED.expires_at
                )
                RETURNING {_RECORD_COLUMNS}
                """,
                user_id,
                operator_id,
                reason,
                expires_at,
                list(scopes),
            )
            current_rows = await conn.fetch(
                f"""
                SELECT {_RECORD_COLUMNS}
                FROM komari_user_bans
                WHERE user_id = $1 AND {_ACTIVE_PREDICATE}
                ORDER BY ban_scope
                """,
                user_id,
            )
            if changed_rows:
                await self._bump_cache_revision(conn)

        if not changed_rows:
            mutation_kind: BanMutationKind = "unchanged"
        elif existing:
            mutation_kind = "updated"
        else:
            mutation_kind = "created"
        affected = tuple(self._row_to_record(row) for row in changed_rows)
        current = tuple(self._row_to_record(row) for row in current_rows)
        return mutation_kind, affected, current

    async def remove_scopes(
        self,
        *,
        user_id: str,
        scopes: tuple[BanScope, ...],
    ) -> tuple[tuple[BanRecord, ...], tuple[BanRecord, ...]]:
        """原子删除一个或多个封禁作用域，并返回删除前内容。"""
        pool = self._require_pool()
        async with pool.acquire() as conn, conn.transaction():
            deleted_rows = await conn.fetch(
                f"""
                DELETE FROM komari_user_bans
                WHERE user_id = $1
                  AND ban_scope = ANY($2::TEXT[])
                  AND {_ACTIVE_PREDICATE}
                RETURNING {_RECORD_COLUMNS}
                """,
                user_id,
                list(scopes),
            )
            current_rows = await conn.fetch(
                f"""
                SELECT {_RECORD_COLUMNS}
                FROM komari_user_bans
                WHERE user_id = $1 AND {_ACTIVE_PREDICATE}
                ORDER BY ban_scope
                """,
                user_id,
            )
            if deleted_rows:
                await self._bump_cache_revision(conn)
        deleted = tuple(self._row_to_record(row) for row in deleted_rows)
        current = tuple(self._row_to_record(row) for row in current_rows)
        return deleted, current

    async def delete_expired(self) -> tuple[BanRecord, ...]:
        """原子删除到期记录，并在同一事务写入自然解封通知 outbox。"""
        pool = self._require_pool()
        async with pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                f"""
                DELETE FROM komari_user_bans
                WHERE expires_at IS NOT NULL
                  AND expires_at <= CURRENT_TIMESTAMP
                RETURNING {_RECORD_COLUMNS}
                """
            )
            records = [self._row_to_record(row) for row in rows]
            records.sort(key=lambda record: (record.user_id, record.ban_scope))
            records_by_user: dict[str, list[BanRecord]] = {}
            for record in records:
                records_by_user.setdefault(record.user_id, []).append(record)
            for user_id, user_records in records_by_user.items():
                await conn.execute(
                    """
                    INSERT INTO komari_user_ban_notification_outbox (
                        notification_id,
                        user_id,
                        notification_kind,
                        records
                    )
                    VALUES ($1, $2, 'natural_expiry', $3::jsonb)
                    """,
                    uuid4().hex,
                    user_id,
                    self._serialize_records(tuple(user_records)),
                )
            if rows:
                await self._bump_cache_revision(conn)
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
        pool = self._require_pool()
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                WITH candidate AS (
                    SELECT notification_id
                    FROM komari_user_ban_notification_outbox
                    WHERE notification_kind = 'natural_expiry'
                      AND records IS NOT NULL
                      AND available_at <= CURRENT_TIMESTAMP
                      AND (
                          status = 'pending'
                          OR (
                              status = 'processing'
                              AND lease_expires_at <= CURRENT_TIMESTAMP
                          )
                      )
                    ORDER BY available_at, created_at, notification_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE komari_user_ban_notification_outbox AS outbox
                SET status = 'processing',
                    owner_token = $1,
                    lease_expires_at = (
                        CURRENT_TIMESTAMP
                        + ($2::double precision * INTERVAL '1 second')
                    ),
                    attempt_count = attempt_count + 1,
                    updated_at = CURRENT_TIMESTAMP
                FROM candidate
                WHERE outbox.notification_id = candidate.notification_id
                RETURNING outbox.notification_id,
                          outbox.user_id,
                          outbox.records,
                          outbox.attempt_count
                """,
                normalized_owner,
                lease_seconds,
            )
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
        pool = self._require_pool()
        async with pool.acquire() as conn:
            acknowledged = await conn.fetchval(
                """
                UPDATE komari_user_ban_notification_outbox
                SET status = 'sent',
                    records = NULL,
                    owner_token = NULL,
                    lease_expires_at = NULL,
                    last_error_code = NULL,
                    sent_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE notification_id = $1
                  AND status = 'processing'
                  AND owner_token = $2
                  AND lease_expires_at > CURRENT_TIMESTAMP
                RETURNING notification_id
                """,
                notification_id,
                owner_token,
            )
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
        pool = self._require_pool()
        async with pool.acquire() as conn:
            retried = await conn.fetchval(
                """
                UPDATE komari_user_ban_notification_outbox
                SET status = 'pending',
                    owner_token = NULL,
                    lease_expires_at = NULL,
                    available_at = (
                        CURRENT_TIMESTAMP
                        + ($4::double precision * INTERVAL '1 second')
                    ),
                    last_error_code = $3,
                    updated_at = CURRENT_TIMESTAMP
                WHERE notification_id = $1
                  AND status = 'processing'
                  AND owner_token = $2
                RETURNING notification_id
                """,
                notification_id,
                owner_token,
                error_code,
                retry_delay_seconds,
            )
        return retried is not None

    async def list_statuses(
        self,
        *,
        scope: BanScope | None,
        limit: int,
        offset: int,
    ) -> tuple[tuple[UserBanStatus, ...], int]:
        """按用户分页列出当前有效的封禁状态。"""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            total = await conn.fetchval(
                f"""
                SELECT COUNT(DISTINCT user_id)
                FROM komari_user_bans
                WHERE ($1::TEXT IS NULL OR ban_scope = $1)
                  AND {_ACTIVE_PREDICATE}
                """,
                scope,
            )
            rows = await conn.fetch(
                f"""
                WITH matching_users AS (
                    SELECT user_id, MAX(updated_at) AS latest_update
                    FROM komari_user_bans
                    WHERE ($1::TEXT IS NULL OR ban_scope = $1)
                      AND {_ACTIVE_PREDICATE}
                    GROUP BY user_id
                    ORDER BY latest_update DESC, user_id
                    LIMIT $2 OFFSET $3
                )
                SELECT bans.user_id,
                       bans.ban_scope,
                       bans.operator_id,
                       bans.reason,
                       bans.expires_at,
                       bans.created_at,
                       bans.updated_at,
                       matching_users.latest_update
                FROM matching_users
                JOIN komari_user_bans AS bans USING (user_id)
                WHERE bans.expires_at IS NULL
                   OR bans.expires_at > CURRENT_TIMESTAMP
                ORDER BY matching_users.latest_update DESC,
                         bans.user_id,
                         bans.ban_scope
                """,
                scope,
                limit,
                offset,
            )
        return self._rows_to_statuses(list(rows)), int(total or 0)
