"""用户封禁 PostgreSQL 访问层。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from komari_bot.common.database_config import get_shared_database_config
from komari_bot.common.postgres import create_postgres_pool

from .models import BanMutationKind, BanRecord, BanScope, UserBanStatus

if TYPE_CHECKING:
    from datetime import datetime

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
        """原子删除所有到期记录，并返回被删除的完整记录。"""
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
            if rows:
                await self._bump_cache_revision(conn)
        records = [self._row_to_record(row) for row in rows]
        records.sort(key=lambda record: (record.user_id, record.ban_scope))
        return tuple(records)

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
