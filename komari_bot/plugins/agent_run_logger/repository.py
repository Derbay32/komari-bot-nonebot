"""Agent Run 日志的 PostgreSQL 轻量索引。

连接来源为 nonebot-plugin-orm 共享引擎（配置统一从 ``SQLALCHEMY_DATABASE_URL``
读取），不再创建自研 asyncpg 池；表结构由 Alembic 迁移管理，文件锁 → JSONL →
PG upsert 顺序、PG 故障不撤销 JSONL、5 分钟 advisory lock 对账等行为契约不变。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nonebot import logger

from komari_bot.common.orm_connection import (
    SharedEngineConnectionPool,
    get_shared_orm_connection_pool,
)

if TYPE_CHECKING:
    from datetime import date, datetime

    import asyncpg

_RECONCILE_LOCK_KEY = 4_861_576_143_022_611_907

_UPSERT_SQL = """
INSERT INTO komari_agent_run_log_index (
    run_id, trace_id, run_type, task_kind, origin, status,
    started_at, finished_at, log_date, file_name, byte_offset, byte_length,
    models, methods, round_count, tool_count,
    input_tokens, cached_input_tokens, cache_miss_input_tokens,
    output_tokens, reasoning_output_tokens, total_tokens, usage_complete
) VALUES (
    $1, $2, $3, $4, $5, $6,
    $7, $8, $9, $10, $11, $12,
    $13, $14, $15, $16,
    $17, $18, $19, $20, $21, $22, $23
)
ON CONFLICT (run_id) DO UPDATE SET
    trace_id = EXCLUDED.trace_id,
    run_type = EXCLUDED.run_type,
    task_kind = EXCLUDED.task_kind,
    origin = EXCLUDED.origin,
    status = EXCLUDED.status,
    started_at = EXCLUDED.started_at,
    finished_at = EXCLUDED.finished_at,
    log_date = EXCLUDED.log_date,
    file_name = EXCLUDED.file_name,
    byte_offset = EXCLUDED.byte_offset,
    byte_length = EXCLUDED.byte_length,
    models = EXCLUDED.models,
    methods = EXCLUDED.methods,
    round_count = EXCLUDED.round_count,
    tool_count = EXCLUDED.tool_count,
    input_tokens = EXCLUDED.input_tokens,
    cached_input_tokens = EXCLUDED.cached_input_tokens,
    cache_miss_input_tokens = EXCLUDED.cache_miss_input_tokens,
    output_tokens = EXCLUDED.output_tokens,
    reasoning_output_tokens = EXCLUDED.reasoning_output_tokens,
    total_tokens = EXCLUDED.total_tokens,
    usage_complete = EXCLUDED.usage_complete
"""


class IndexUnavailableError(RuntimeError):
    """PostgreSQL 日志索引暂不可用。"""

    def __init__(self) -> None:
        super().__init__("PostgreSQL 日志索引不可用")


@dataclass(frozen=True, slots=True)
class AgentRunIndexEntry:
    """一条 JSONL 物理记录的可重建定位元数据。"""

    run_id: str
    trace_id: str
    run_type: str
    task_kind: str
    origin: str
    status: str
    started_at: datetime
    finished_at: datetime
    log_date: date
    file_name: str
    byte_offset: int
    byte_length: int
    models: list[str]
    methods: list[str]
    round_count: int
    tool_count: int
    input_tokens: int
    cached_input_tokens: int
    cache_miss_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int
    usage_complete: bool

    def sql_values(self) -> tuple[object, ...]:
        return (
            self.run_id,
            self.trace_id,
            self.run_type,
            self.task_kind,
            self.origin,
            self.status,
            self.started_at,
            self.finished_at,
            self.log_date,
            self.file_name,
            self.byte_offset,
            self.byte_length,
            self.models,
            self.methods,
            self.round_count,
            self.tool_count,
            self.input_tokens,
            self.cached_input_tokens,
            self.cache_miss_input_tokens,
            self.output_tokens,
            self.reasoning_output_tokens,
            self.total_tokens,
            self.usage_complete,
        )


def _entry_from_record(record: asyncpg.Record | dict[str, Any]) -> AgentRunIndexEntry:
    return AgentRunIndexEntry(
        run_id=str(record["run_id"]),
        trace_id=str(record["trace_id"]),
        run_type=str(record["run_type"]),
        task_kind=str(record["task_kind"]),
        origin=str(record["origin"]),
        status=str(record["status"]),
        started_at=record["started_at"],
        finished_at=record["finished_at"],
        log_date=record["log_date"],
        file_name=str(record["file_name"]),
        byte_offset=int(record["byte_offset"]),
        byte_length=int(record["byte_length"]),
        models=list(record["models"] or []),
        methods=list(record["methods"] or []),
        round_count=int(record["round_count"]),
        tool_count=int(record["tool_count"]),
        input_tokens=int(record["input_tokens"]),
        cached_input_tokens=int(record["cached_input_tokens"]),
        cache_miss_input_tokens=int(record["cache_miss_input_tokens"]),
        output_tokens=int(record["output_tokens"]),
        reasoning_output_tokens=int(record["reasoning_output_tokens"]),
        total_tokens=int(record["total_tokens"]),
        usage_complete=bool(record["usage_complete"]),
    )


class AgentRunIndexRepository:
    """管理不含任何日志正文的 PostgreSQL 索引。"""

    def __init__(self) -> None:
        self._pool: SharedEngineConnectionPool | None = None
        self._init_lock = asyncio.Lock()
        self._available = False

    @property
    def available(self) -> bool:
        return self._available and self._pool is not None

    async def initialize(self) -> bool:
        """探测共享引擎可达性；失败仅使查询降级，不影响 JSONL。"""
        async with self._init_lock:
            try:
                if self._pool is None:
                    self._pool = get_shared_orm_connection_pool()
                self._available = await self._pool.probe()
            except Exception:
                self._available = False
                logger.opt(exception=True).warning(
                    "[AgentRunLogger] PostgreSQL 索引初始化失败，已降级为 JSONL 扫描"
                )
                return False

        return True

    async def _require_pool(self) -> SharedEngineConnectionPool:
        if not self.available and not await self.initialize():
            raise IndexUnavailableError
        if self._pool is None:
            raise IndexUnavailableError
        return self._pool

    def _mark_failed(self) -> None:
        self._available = False

    async def upsert_many(self, entries: list[AgentRunIndexEntry]) -> bool:
        if not entries:
            return True
        try:
            pool = await self._require_pool()
            async with pool.acquire() as connection:
                await connection.executemany(
                    _UPSERT_SQL,
                    [entry.sql_values() for entry in entries],
                )
        except Exception:
            self._mark_failed()
            logger.opt(exception=True).warning(
                "[AgentRunLogger] JSONL 已写入，但 PostgreSQL 索引更新失败"
            )
            return False
        else:
            return True

    async def reconcile(
        self,
        entries: list[AgentRunIndexEntry],
        *,
        retained_from: date,
    ) -> bool:
        """在 advisory transaction lock 内补齐索引并移除陈旧定位。"""
        try:
            pool = await self._require_pool()
            async with pool.acquire() as connection, connection.transaction():
                locked = await connection.fetchval(
                    "SELECT pg_try_advisory_xact_lock($1)",
                    _RECONCILE_LOCK_KEY,
                )
                if not locked:
                    return False
                if entries:
                    await connection.executemany(
                        _UPSERT_SQL,
                        [entry.sql_values() for entry in entries],
                    )
                live_ids = [entry.run_id for entry in entries]
                await connection.execute(
                    """
                    DELETE FROM komari_agent_run_log_index
                    WHERE log_date >= $1
                      AND NOT (run_id = ANY($2::text[]))
                    """,
                    retained_from,
                    live_ids,
                )
                await connection.execute(
                    """
                    DELETE FROM komari_agent_run_log_index
                    WHERE log_date < $1
                    """,
                    retained_from,
                )
        except Exception:
            self._mark_failed()
            logger.opt(exception=True).warning(
                "[AgentRunLogger] PostgreSQL 日志索引对账失败"
            )
            return False
        else:
            return True

    async def delete_before(self, retained_from: date) -> bool:
        try:
            pool = await self._require_pool()
            async with pool.acquire() as connection:
                await connection.execute(
                    "DELETE FROM komari_agent_run_log_index WHERE log_date < $1",
                    retained_from,
                )
        except Exception:
            self._mark_failed()
            return False
        else:
            return True

    async def get(self, run_id: str) -> AgentRunIndexEntry | None:
        try:
            pool = await self._require_pool()
            async with pool.acquire() as connection:
                record = await connection.fetchrow(
                    """
                    SELECT * FROM komari_agent_run_log_index
                    WHERE run_id = $1
                    """,
                    run_id,
                )
            return None if record is None else _entry_from_record(record)
        except Exception as error:
            self._mark_failed()
            if isinstance(error, IndexUnavailableError):
                raise
            raise IndexUnavailableError from error

    async def list_entries(
        self,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
        run_type: str | None = None,
        task_kind: str | None = None,
        origin: str | None = None,
        trace_id: str | None = None,
        status: str | None = None,
        model: str | None = None,
        method: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[AgentRunIndexEntry], int]:
        conditions: list[str] = []
        values: list[object] = []

        def add(condition: str, value: object) -> None:
            values.append(value)
            conditions.append(condition.format(index=len(values)))

        if date_from is not None:
            add("log_date >= ${index}", date_from)
        if date_to is not None:
            add("log_date <= ${index}", date_to)
        if run_type is not None:
            add("run_type = ${index}", run_type)
        if task_kind is not None:
            add("task_kind = ${index}", task_kind)
        if origin is not None:
            add("origin = ${index}", origin)
        if trace_id is not None:
            add("trace_id = ${index}", trace_id)
        if status is not None:
            add("status = ${index}", status)
        if model is not None:
            add("${index} = ANY(models)", model)
        if method is not None:
            add("${index} = ANY(methods)", method)

        where = "" if not conditions else f" WHERE {' AND '.join(conditions)}"
        try:
            pool = await self._require_pool()
            async with pool.acquire() as connection:
                total = int(
                    (await connection.fetchval(
                        f"SELECT COUNT(*) FROM komari_agent_run_log_index{where}",
                        *values,
                    ))
                    or 0
                )
                limit_index = len(values) + 1
                offset_index = len(values) + 2
                records = await connection.fetch(
                    f"""
                    SELECT * FROM komari_agent_run_log_index
                    {where}
                    ORDER BY started_at DESC, run_id DESC
                    LIMIT ${limit_index} OFFSET ${offset_index}
                    """,
                    *values,
                    limit,
                    offset,
                )
            return [_entry_from_record(record) for record in records], total
        except Exception as error:
            self._mark_failed()
            if isinstance(error, IndexUnavailableError):
                raise
            raise IndexUnavailableError from error

    async def close(self) -> None:
        async with self._init_lock:
            pool = self._pool
            self._pool = None
            self._available = False
            if pool is not None:
                # 共享引擎由 nonebot-plugin-orm 托管，close 仅为释放本仓库引用
                await pool.close()


repository = AgentRunIndexRepository()


__all__ = [
    "AgentRunIndexEntry",
    "AgentRunIndexRepository",
    "IndexUnavailableError",
    "repository",
]
