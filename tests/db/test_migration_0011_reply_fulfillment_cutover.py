"""迁移 0011 回复履约原子切换的真实 PostgreSQL 验收。

隔离纪律：0011 不可逆，链驱动验收不得搬移共享门控库的版本。
每个用例在从门控 DSN 派生的一次性隔离库（库名后缀 ``_mig0011``）
内重建迁移链，用例结束即 DROP；共享门控库始终保持 head，
重复执行与执行顺序互不影响。门控用户需要 CREATEDB 权限。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import asyncpg
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")
SQLALCHEMY_URL = os.getenv("SQLALCHEMY_DATABASE_URL", "")

pytestmark = [
    pytest.mark.skipif(
        not POSTGRES_URL,
        reason="未设置 KOMARI_TEST_POSTGRES_URL，跳过迁移集成测试",
    ),
    pytest.mark.asyncio,
]

OLD_CONFIG_COLUMNS = (
    "reply_commit_worker_interval_seconds",
    "reply_commit_batch_size",
    "reply_commit_lease_seconds",
    "reply_commit_max_attempts",
    "reply_commit_retry_base_seconds",
    "reply_commit_tombstone_retention_days",
)
NEW_CONFIG_COLUMNS = (
    "reply_fulfillment_worker_interval_seconds",
    "reply_fulfillment_batch_size",
    "reply_fulfillment_lease_seconds",
    "reply_fulfillment_max_attempts",
    "reply_fulfillment_retry_base_seconds",
    "reply_fulfillment_retry_max_seconds",
    "reply_fulfillment_tombstone_retention_days",
)
DISTINCTIVE_VALUES = (17, 9, 121, 8, 13, 44)


def _same_database(left: str, right: str) -> bool:
    left_parsed = urlparse(left)
    right_parsed = urlparse(right)
    return (
        left_parsed.hostname == right_parsed.hostname
        and (left_parsed.port or 5432) == (right_parsed.port or 5432)
        and left_parsed.path == right_parsed.path
    )


def _parse_dsn(url: str) -> dict[str, Any]:
    parsed = urlparse(url.replace("postgresql+asyncpg://", "postgresql://"))
    return {
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "database": parsed.path.lstrip("/"),
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
    }


def _run_bootstrap(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["SQLALCHEMY_DATABASE_URL"] = url
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "komari_bot.db.orm_bootstrap", *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


def _scratch_url(database: str) -> str:
    """把门控 DSN 的库名替换为隔离库名，其余连接参数保持不变。"""
    return urlparse(POSTGRES_URL)._replace(path=f"/{database}").geturl()


async def _recreate_scratch_database() -> dict[str, Any]:
    """重建本文件的一次性隔离库并返回其 asyncpg 连接参数。

    隔离库名 = 门控库名 + ``_mig0011``；先 DROP（FORCE 断开残留
    连接）再 CREATE，重复执行幂等。门控用户需要 CREATEDB 权限。
    """
    base = _parse_dsn(POSTGRES_URL)
    scratch = {**base, "database": f"{base['database']}_mig0011"}
    connection = await asyncpg.connect(**base)
    try:
        name = str(scratch["database"]).replace('"', '""')
        await connection.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await connection.execute(f'CREATE DATABASE "{name}"')
    finally:
        await connection.close()
    return scratch


async def _drop_scratch_database(database: str) -> None:
    """删除一次性隔离库（finally 清理，重复删除安全）。"""
    base = _parse_dsn(POSTGRES_URL)
    connection = await asyncpg.connect(**base)
    try:
        name = database.replace('"', '""')
        await connection.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await connection.close()


async def _table_exists(connection: asyncpg.Connection, table_name: str) -> bool:
    return bool(
        await connection.fetchval(
            "SELECT to_regclass($1) IS NOT NULL",
            table_name,
        )
    )


async def _column_names(
    connection: asyncpg.Connection,
    table_name: str,
) -> set[str]:
    rows = await connection.fetch(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = $1
        """,
        table_name,
    )
    return {str(row["column_name"]) for row in rows}


async def _ensure_chat_config_row(connection: asyncpg.Connection) -> bool:
    exists = await connection.fetchval(
        "SELECT EXISTS (SELECT 1 FROM komari_chat_config WHERE id = 1)"
    )
    if exists:
        return False
    columns = await _column_names(connection, "komari_chat_config")
    value_columns = [
        column
        for column in sorted(columns)
        if column not in {"id", "revision", "updated_at"}
    ]
    defaults: dict[str, object] = {
        "proactive_enabled": False,
        "proactive_cooldown": 300,
        "proactive_max_per_hour": 400,
        "proactive_reservation_ttl_seconds": 360,
        "reply_commit_worker_interval_seconds": 5,
        "reply_commit_batch_size": 20,
        "reply_commit_lease_seconds": 120,
        "reply_commit_max_attempts": 20,
        "reply_commit_retry_base_seconds": 5,
        "reply_commit_tombstone_retention_days": 30,
        "reply_fulfillment_freshness_seconds": 120,
    }
    columns_sql = ", ".join(["id", "revision", "updated_at", *value_columns])
    placeholders = ", ".join(f"${index}" for index in range(1, 4 + len(value_columns)))
    await connection.execute(
        f"INSERT INTO komari_chat_config ({columns_sql}) VALUES ({placeholders})",
        1,
        1,
        datetime.now(UTC),
        *[defaults[column] for column in value_columns],
    )
    return True


async def _insert_legacy_prepared(
    connection: asyncpg.Connection,
    fulfillment_id: str,
) -> None:
    now = datetime.now(UTC)
    await connection.execute(
        """
        INSERT INTO komari_chat_reply_commit_outbox (
            operation_id,
            payload_hash,
            request_trace_id,
            source_message_id,
            group_id,
            user_id,
            user_nickname,
            bot_nickname,
            reply_content,
            reply_timestamp,
            favorability_delta,
            favorability_reason,
            interaction_history,
            proactive_reservation_id,
            proactive_cooldown_seconds,
            global_interaction_enabled,
            global_interaction_trigger_size,
            status,
            created_at,
            updated_at,
            delivery_state,
            bot_self_id,
            adapter_name,
            reply_target_message_id,
            prepared_at
        )
        VALUES (
            $1, $2, $3, $4, 'group-1', 'user-1', '不得泄露昵称', '小鞠',
            '不得泄露的旧回复正文', 123.5, 1, '正常互动', $5::jsonb,
            'reservation-1', 300, true, 20, 'PREPARED', $6, $6,
            'NOT_STARTED', 'bot-1', 'OneBot V11', $4, $6
        )
        """,
        fulfillment_id,
        "f" * 64,
        f"trace-{fulfillment_id}",
        f"message-{fulfillment_id}",
        json.dumps(
            {"event": "发言", "result": "回复", "emotion": "平静"},
            ensure_ascii=False,
        ),
        now,
    )


async def _insert_parent_for_legacy(
    connection: asyncpg.Connection,
    fulfillment_id: str,
) -> None:
    await connection.execute(
        """
        INSERT INTO komari_chat_reply_fulfillments (
            fulfillment_id,
            payload_hash,
            request_trace_id,
            trigger_message_id,
            trigger_user_id,
            group_id,
            bot_self_id,
            adapter_name,
            reply_target_message_id,
            reply_content,
            delivery_state,
            send_started_at
        )
        VALUES (
            $1, $2, $3, $4, 'user-1', 'group-1', 'bot-1', 'OneBot V11',
            $4, '不得泄露的旧回复正文', 'PENDING_CONFIRMATION', NOW()
        )
        """,
        fulfillment_id,
        "f" * 64,
        f"trace-{fulfillment_id}",
        f"message-{fulfillment_id}",
    )


async def _insert_parent_children_for_legacy(
    connection: asyncpg.Connection,
    fulfillment_id: str,
) -> None:
    await _insert_parent_for_legacy(connection, fulfillment_id)
    await connection.executemany(
        """
        INSERT INTO komari_chat_reply_fulfillment_commitments (
            fulfillment_id,
            commitment_type,
            payload
        )
        VALUES ($1, $2, $3::jsonb)
        """,
        [
            (
                fulfillment_id,
                "proactive_reply_confirmation",
                json.dumps(
                    {
                        "group_id": "group-1",
                        "reservation_id": "reservation-1",
                        "cooldown_seconds": 300,
                    },
                    ensure_ascii=False,
                ),
            ),
            (
                fulfillment_id,
                "favorability_adjustment",
                json.dumps(
                    {"user_id": "user-1", "delta": 1, "reason": "正常互动"},
                    ensure_ascii=False,
                ),
            ),
            (
                fulfillment_id,
                "assistant_reply_history",
                json.dumps(
                    {
                        "group_id": "group-1",
                        "bot_nickname": "小鞠",
                        "reply_content": "不得泄露的旧回复正文",
                        "reply_timestamp": 123.5,
                    },
                    ensure_ascii=False,
                ),
            ),
            (
                fulfillment_id,
                "interaction_history",
                json.dumps(
                    {
                        "user_id": "user-1",
                        "display_name": "不得泄露昵称",
                        "trigger_size": 20,
                        "reply_timestamp": 123.5,
                        "trigger_message_id": f"message-{fulfillment_id}",
                        "record": {
                            "event": "发言",
                            "result": "回复",
                            "emotion": "平静",
                        },
                    },
                    ensure_ascii=False,
                ),
            ),
        ],
    )


async def test_cutover_aborts_and_rolls_back_when_backfill_is_missing() -> None:
    """0010 后新增旧行没有父镜像时，0011 fail-fast 且整笔事务回滚。"""
    if not _same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")

    scratch = await _recreate_scratch_database()
    scratch_url = _scratch_url(str(scratch["database"]))
    result = _run_bootstrap(scratch_url, "upgrade", "0010")
    assert result.returncode == 0, result.stderr
    connection = await asyncpg.connect(**scratch)
    fulfillment_id = "tsk87-missing-parent"
    try:
        await _insert_legacy_prepared(connection, fulfillment_id)

        result = _run_bootstrap(scratch_url, "upgrade", "head")
        assert result.returncode != 0
        output = f"{result.stdout}\n{result.stderr}"
        assert "missing_backfill_count=1" in output
        assert f"minimum_fulfillment_id={fulfillment_id}" in output
        assert "不得泄露的旧回复正文" not in output
        assert "不得泄露昵称" not in output

        assert (
            await connection.fetchval("SELECT version_num FROM alembic_version")
            == "0010"
        )
        assert await _table_exists(connection, "komari_chat_reply_commit_outbox")
        columns = await _column_names(connection, "komari_chat_config")
        assert set(OLD_CONFIG_COLUMNS) <= columns
        assert not set(NEW_CONFIG_COLUMNS).intersection(columns)
        assert (
            await connection.fetchval(
                "SELECT COUNT(*) FROM komari_chat_reply_commit_outbox WHERE operation_id = $1",
                fulfillment_id,
            )
            == 1
        )
    finally:
        await connection.close()
        await _drop_scratch_database(str(scratch["database"]))


async def test_cutover_aborts_when_parent_child_mirror_is_incomplete() -> None:
    """父身份存在但子项数量不完整时，不得删除旧表或静默丢承诺。"""
    if not _same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")

    scratch = await _recreate_scratch_database()
    scratch_url = _scratch_url(str(scratch["database"]))
    result = _run_bootstrap(scratch_url, "upgrade", "0010")
    assert result.returncode == 0, result.stderr
    connection = await asyncpg.connect(**scratch)
    fulfillment_id = "tsk87-missing-children"
    try:
        await _insert_legacy_prepared(connection, fulfillment_id)
        await _insert_parent_for_legacy(connection, fulfillment_id)

        result = _run_bootstrap(scratch_url, "upgrade", "head")
        assert result.returncode != 0
        output = f"{result.stdout}\n{result.stderr}"
        assert "commitment_mismatch_count=1" in output
        assert f"minimum_fulfillment_id={fulfillment_id}" in output
        assert "不得泄露的旧回复正文" not in output

        assert (
            await connection.fetchval("SELECT version_num FROM alembic_version")
            == "0010"
        )
        assert await _table_exists(connection, "komari_chat_reply_commit_outbox")
    finally:
        await connection.close()
        await _drop_scratch_database(str(scratch["database"]))


async def test_cutover_preserves_config_and_drops_only_complete_legacy_table() -> None:
    """完整父子镜像通过门禁，配置值一对一保留且旧表被删除。

    本用例在独立的隔离库内把迁移链推进到不可逆的 0011；隔离库随
    用例结束删除，不污染共享门控库，因此用例间无执行顺序要求。
    """
    if not _same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")

    scratch = await _recreate_scratch_database()
    scratch_url = _scratch_url(str(scratch["database"]))
    result = _run_bootstrap(scratch_url, "upgrade", "0010")
    assert result.returncode == 0, result.stderr
    connection = await asyncpg.connect(**scratch)
    fulfillment_id = "tsk87-complete-cutover"
    try:
        await _ensure_chat_config_row(connection)
        set_clause = ", ".join(
            f"{column} = ${index}"
            for index, column in enumerate(OLD_CONFIG_COLUMNS, start=1)
        )
        await connection.execute(
            f"UPDATE komari_chat_config SET {set_clause} WHERE id = 1",
            *DISTINCTIVE_VALUES,
        )
        await _insert_legacy_prepared(connection, fulfillment_id)
        await _insert_parent_children_for_legacy(connection, fulfillment_id)

        result = _run_bootstrap(scratch_url, "upgrade", "head")
        assert result.returncode == 0, result.stderr

        assert not await _table_exists(connection, "komari_chat_reply_commit_outbox")
        assert await _table_exists(connection, "komari_chat_reply_fulfillments")
        assert await _table_exists(
            connection, "komari_chat_reply_fulfillment_commitments"
        )
        columns = await _column_names(connection, "komari_chat_config")
        assert set(NEW_CONFIG_COLUMNS) <= columns
        assert not set(OLD_CONFIG_COLUMNS).intersection(columns)
        row = await connection.fetchrow(
            """
            SELECT reply_fulfillment_worker_interval_seconds,
                   reply_fulfillment_batch_size,
                   reply_fulfillment_lease_seconds,
                   reply_fulfillment_max_attempts,
                   reply_fulfillment_retry_base_seconds,
                   reply_fulfillment_retry_max_seconds,
                   reply_fulfillment_tombstone_retention_days
            FROM komari_chat_config
            WHERE id = 1
            """
        )
        assert row is not None
        assert tuple(row) == (*DISTINCTIVE_VALUES[:5], 3600, DISTINCTIVE_VALUES[5])
        assert (
            await connection.fetchval("SELECT version_num FROM alembic_version")
            == "0011"
        )
        assert (
            await connection.fetchval(
                """
                SELECT COUNT(*)
                FROM komari_chat_reply_fulfillment_commitments
                WHERE fulfillment_id = $1
                """,
                fulfillment_id,
            )
            == 4
        )
    finally:
        await connection.close()
        await _drop_scratch_database(str(scratch["database"]))
