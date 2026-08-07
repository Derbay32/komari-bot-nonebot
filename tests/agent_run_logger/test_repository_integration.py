"""AgentRunIndexRepository 共享引擎连接集成测试。

取代 ``test_storage_reader_repository.py`` 中 ``_ConfigPool`` fake 连接测试：
upsert 幂等、按任意字段筛选、advisory xact lock 对账、保留期删除等行为全部
通过仓储公共接口在真实库上断言。写入的 run_id 带 ``t10-red-`` 前缀，收尾
同时清理门控库与旧配置指向的库，避免遗留脏数据。
"""

from __future__ import annotations

import os
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlparse
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

import pytest

from komari_bot.plugins.agent_run_logger.repository import (
    AgentRunIndexEntry,
    AgentRunIndexRepository,
)

if TYPE_CHECKING:
    import asyncpg

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")

_RECONCILE_LOCK_KEY = 4_861_576_143_022_611_907


def _configured_database_url() -> str:
    from nonebot import get_driver

    return str(getattr(get_driver().config, "sqlalchemy_database_url", "") or "")


def _same_database(left: str, right: str) -> bool:
    left_parsed = urlparse(left)
    right_parsed = urlparse(right)
    return (
        left_parsed.hostname == right_parsed.hostname
        and (left_parsed.port or 5432) == (right_parsed.port or 5432)
        and left_parsed.path == right_parsed.path
    )


def _asyncpg_url() -> str:
    return POSTGRES_URL.replace("postgresql+asyncpg://", "postgresql://")


async def _reset_shared_orm_engine() -> None:
    from nonebot import require

    require("nonebot_plugin_orm")
    import nonebot_plugin_orm as orm_module

    engines = getattr(orm_module, "_engines", None)
    if not engines:
        return
    for engine in list(engines.values()):
        with suppress(Exception):
            await engine.dispose()


def _make_entry(
    run_id: str,
    *,
    log_date: date,
    status: str = "success",
    models: list[str] | None = None,
    methods: list[str] | None = None,
    started: datetime | None = None,
) -> AgentRunIndexEntry:
    started_at = started or datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
    return AgentRunIndexEntry(
        run_id=run_id,
        trace_id=f"trace-{run_id}",
        run_type="chat_reply",
        task_kind="chat_reply",
        origin="normal",
        status=status,
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=1),
        log_date=log_date,
        file_name=f"{log_date.isoformat()}.jsonl",
        byte_offset=0,
        byte_length=100,
        models=models or ["deepseek-chat"],
        methods=methods or ["generate_messages_completion"],
        round_count=1,
        tool_count=0,
        input_tokens=10,
        cached_input_tokens=2,
        cache_miss_input_tokens=8,
        output_tokens=5,
        reasoning_output_tokens=1,
        total_tokens=15,
        usage_complete=True,
    )


async def _admin_connect() -> "asyncpg.Connection":
    import asyncpg

    return await asyncpg.connect(_asyncpg_url())


async def _cleanup_run_ids(run_ids: set[str]) -> None:
    """从门控库清理测试写入的索引行（v2.0.0 起连接唯一来源是 SQLALCHEMY_DATABASE_URL）。"""
    import asyncpg

    targets: list[str] = [_asyncpg_url()]
    for target in targets:
        try:
            admin = await asyncpg.connect(target)
        except Exception:
            continue
        try:
            await admin.execute(
                "DELETE FROM komari_agent_run_log_index WHERE run_id = ANY($1::text[])",
                sorted(run_ids),
            )
        except Exception:
            pass
        finally:
            await admin.close()


@pytest.fixture
async def repository() -> "AsyncIterator[tuple[AgentRunIndexRepository, set[str]]]":
    await _reset_shared_orm_engine()
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    repo = AgentRunIndexRepository()
    created_run_ids: set[str] = set()
    yield (repo, created_run_ids)
    await repo.close()
    await _cleanup_run_ids(created_run_ids)
    await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_initialize_uses_configured_database(repository: tuple[AgentRunIndexRepository, set[str]]) -> None:
    repo, created_run_ids = repository
    assert await repo.initialize() is True
    assert repo.available is True

    # 验收：upsert 必须落在 nonebot-plugin-orm 配置的数据库
    run_id = f"t10-red-{uuid4().hex}"
    created_run_ids.add(run_id)
    assert await repo.upsert_many(
        [_make_entry(run_id, log_date=date(2026, 7, 22))]
    ) is True

    admin = await _admin_connect()
    try:
        found = await admin.fetchval(
            "SELECT COUNT(*) FROM komari_agent_run_log_index WHERE run_id = $1",
            run_id,
        )
    finally:
        await admin.close()
    assert int(found or 0) == 1


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_upsert_many_is_idempotent_and_get_returns_entry(
    repository: tuple[AgentRunIndexRepository, set[str]],
) -> None:
    repo, created_run_ids = repository
    assert await repo.initialize() is True
    run_id = f"t10-red-{uuid4().hex}"
    created_run_ids.add(run_id)
    entry = _make_entry(run_id, log_date=date(2026, 7, 22))

    assert await repo.upsert_many([entry]) is True
    # 重复写入同 run_id 应覆盖而非报错
    assert await repo.upsert_many([entry]) is True

    fetched = await repo.get(run_id)
    assert fetched is not None
    assert fetched.run_id == run_id
    assert fetched.trace_id == f"trace-{run_id}"
    assert fetched.models == ["deepseek-chat"]
    assert fetched.methods == ["generate_messages_completion"]
    assert fetched.usage_complete is True
    assert await repo.get("t10-red-missing") is None


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_list_entries_filters_and_pagination(repository: tuple[AgentRunIndexRepository, set[str]]) -> None:
    repo, created_run_ids = repository
    assert await repo.initialize() is True
    prefix = f"t10-red-{uuid4().hex}"
    for index in range(3):
        run_id = f"{prefix}-{index}"
        created_run_ids.add(run_id)
        await repo.upsert_many(
            [
                _make_entry(
                    run_id,
                    log_date=date(2026, 7, 22),
                    status="success" if index < 2 else "failed",
                    models=["model-a"] if index == 0 else ["model-b"],
                    methods=["method-x"] if index == 1 else ["method-y"],
                    started=datetime(2026, 7, 22, 10, index, tzinfo=UTC),
                )
            ]
        )

    items, total = await repo.list_entries(
        date_from=date(2026, 7, 22),
        date_to=date(2026, 7, 22),
        limit=10,
        offset=0,
    )
    assert total == 3
    assert {item.run_id for item in items} == {
        f"{prefix}-{index}" for index in range(3)
    }

    by_status, total_s = await repo.list_entries(status="failed", limit=10, offset=0)
    assert total_s == 1
    assert by_status[0].run_id == f"{prefix}-2"

    by_model, total_m = await repo.list_entries(model="model-a", limit=10, offset=0)
    assert total_m == 1
    assert by_model[0].run_id == f"{prefix}-0"

    by_method, total_md = await repo.list_entries(
        method="method-x", limit=10, offset=0
    )
    assert total_md == 1
    assert by_method[0].run_id == f"{prefix}-1"

    _by_trace, total_t = await repo.list_entries(
        trace_id=f"trace-{prefix}-1", limit=10, offset=0
    )
    assert total_t == 1

    page, total_p = await repo.list_entries(limit=2, offset=1)
    assert len(page) == 2
    assert total_p == 3


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_reconcile_repairs_and_removes_stale_rows(repository: tuple[AgentRunIndexRepository, set[str]]) -> None:
    repo, created_run_ids = repository
    assert await repo.initialize() is True
    prefix = f"t10-red-{uuid4().hex}"
    retained_from = date(2026, 7, 22)

    # 预置：陈旧行（保留期内但不在 live 集合）、过期行（保留期之前）
    admin = await _admin_connect()
    try:
        await admin.execute(
            "DELETE FROM komari_agent_run_log_index WHERE run_id LIKE $1",
            f"{prefix}-%",
        )
        stale_run = f"{prefix}-stale"
        old_run = f"{prefix}-old"
        created_run_ids.add(stale_run)
        created_run_ids.add(old_run)
        await admin.execute(
            """
            INSERT INTO komari_agent_run_log_index (
                run_id, trace_id, run_type, task_kind, origin, status,
                started_at, finished_at, log_date, file_name,
                byte_offset, byte_length, models, methods,
                round_count, tool_count, input_tokens, cached_input_tokens,
                cache_miss_input_tokens, output_tokens, reasoning_output_tokens,
                total_tokens, usage_complete
            ) VALUES (
                $1, 'trace-stale', 'chat_reply', 'chat_reply', 'normal', 'success',
                $2, $2, $3, 'x.jsonl', 0, 1, ARRAY['m']::text[], ARRAY['x']::text[],
                1, 0, 0, 0, 0, 0, 0, 0, FALSE
            )
            """,
            stale_run,
            datetime(2026, 7, 22, 9, 0, tzinfo=UTC),
            retained_from,
        )
        await admin.execute(
            """
            INSERT INTO komari_agent_run_log_index (
                run_id, trace_id, run_type, task_kind, origin, status,
                started_at, finished_at, log_date, file_name,
                byte_offset, byte_length, models, methods,
                round_count, tool_count, input_tokens, cached_input_tokens,
                cache_miss_input_tokens, output_tokens, reasoning_output_tokens,
                total_tokens, usage_complete
            ) VALUES (
                $1, 'trace-old', 'chat_reply', 'chat_reply', 'normal', 'success',
                $2, $2, $3, 'x.jsonl', 0, 1, ARRAY['m']::text[], ARRAY['x']::text[],
                1, 0, 0, 0, 0, 0, 0, 0, FALSE
            )
            """,
            old_run,
            datetime(2026, 7, 21, 9, 0, tzinfo=UTC),
            date(2026, 7, 21),
        )
    finally:
        await admin.close()

    live_run = f"{prefix}-live"
    created_run_ids.add(live_run)
    reconciled = await repo.reconcile(
        [_make_entry(live_run, log_date=retained_from)],
        retained_from=retained_from,
    )
    assert reconciled is True

    admin = await _admin_connect()
    try:
        remaining = await admin.fetch(
            "SELECT run_id FROM komari_agent_run_log_index WHERE run_id LIKE $1",
            f"{prefix}-%",
        )
    finally:
        await admin.close()
    assert sorted(row["run_id"] for row in remaining) == [live_run]


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_reconcile_skips_when_another_transaction_holds_advisory_lock(
    repository: tuple[AgentRunIndexRepository, set[str]],
) -> None:
    repo, created_run_ids = repository
    assert await repo.initialize() is True
    run_id = f"t10-red-{uuid4().hex}"
    created_run_ids.add(run_id)

    import asyncpg

    admin = await asyncpg.connect(_asyncpg_url())
    try:
        async with admin.transaction():
            locked = await admin.fetchval(
                "SELECT pg_try_advisory_xact_lock($1)", _RECONCILE_LOCK_KEY
            )
            assert locked is True
            skipped = await repo.reconcile(
                [_make_entry(run_id, log_date=date(2026, 7, 22))],
                retained_from=date(2026, 7, 22),
            )
            assert skipped is False
    finally:
        await admin.close()

    # 锁释放后对账恢复成功
    assert await repo.reconcile(
        [_make_entry(run_id, log_date=date(2026, 7, 22))],
        retained_from=date(2026, 7, 22),
    ) is True


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_delete_before_removes_expired_rows(repository: tuple[AgentRunIndexRepository, set[str]]) -> None:
    repo, created_run_ids = repository
    assert await repo.initialize() is True
    prefix = f"t10-red-{uuid4().hex}"
    kept_run = f"{prefix}-kept"
    expired_run = f"{prefix}-expired"
    created_run_ids.add(kept_run)
    created_run_ids.add(expired_run)

    await repo.upsert_many(
        [
            _make_entry(kept_run, log_date=date(2026, 7, 22)),
            _make_entry(expired_run, log_date=date(2026, 7, 21)),
        ]
    )
    assert await repo.delete_before(date(2026, 7, 22)) is True
    assert await repo.get(expired_run) is None
    assert await repo.get(kept_run) is not None


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_close_releases_reference_without_disposing_shared_engine(
    repository: tuple[AgentRunIndexRepository, set[str]],
) -> None:
    repo, _created_run_ids = repository
    assert await repo.initialize() is True
    await repo.close()
    assert repo.available is False
    # 共享引擎仍可继续服务其他调用方
    from komari_bot.db.orm_connection import get_shared_orm_connection_pool

    pool = get_shared_orm_connection_pool()
    async with pool.acquire() as conn:  # type: ignore[attr-defined]
        value = await conn.fetchval("SELECT 1")
        assert value == 1
