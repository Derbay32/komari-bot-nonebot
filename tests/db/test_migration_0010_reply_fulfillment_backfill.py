"""迁移 0010 旧回复履约回填的真实 PostgreSQL 验收。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
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


def _run_bootstrap(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["SQLALCHEMY_DATABASE_URL"] = POSTGRES_URL
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


async def _require_empty_reply_tables(connection: asyncpg.Connection) -> None:
    counts = await connection.fetchrow(
        """
        SELECT
            (SELECT COUNT(*) FROM komari_chat_reply_commit_outbox) AS legacy_count,
            (SELECT COUNT(*) FROM komari_chat_reply_fulfillments) AS parent_count,
            (
                SELECT COUNT(*)
                FROM komari_chat_reply_fulfillment_commitments
            ) AS child_count
        """
    )
    assert counts is not None
    if any(int(counts[key]) for key in counts):
        pytest.skip("回复履约迁移验收要求专用空测试库")


async def _insert_legacy_row(
    connection: asyncpg.Connection,
    *,
    operation_id: str,
    payload_hash: str,
    status: str,
    delivery_state: str,
    now: datetime,
    proactive_reservation_id: str | None = "reservation-1",
    global_interaction_enabled: bool = True,
    proactive_confirmed_at: datetime | None = None,
    favorability_applied_at: datetime | None = None,
    ai_history_stored_at: datetime | None = None,
    interaction_stored_at: datetime | None = None,
    attempt_count: int = 0,
    next_retry_at: datetime | None = None,
    lease_owner: str | None = None,
    lease_expires_at: datetime | None = None,
    last_error_code: str | None = None,
    completed_at: datetime | None = None,
    updated_at: datetime | None = None,
    send_started_at: datetime | None = None,
    delivered_at: datetime | None = None,
    not_delivered_at: datetime | None = None,
    terminal_payload_minimized: bool = False,
) -> None:
    user_nickname = None if terminal_payload_minimized else "测试用户"
    bot_nickname = None if terminal_payload_minimized else "小鞠"
    reply_content = None if terminal_payload_minimized else f"回复正文-{operation_id}"
    favorability_reason = None if terminal_payload_minimized else "正常互动"
    interaction_history = None
    if not terminal_payload_minimized:
        interaction_history = json.dumps(
            {"event": "用户发言", "result": "角色回复", "emotion": "平静"},
            ensure_ascii=False,
        )
    if terminal_payload_minimized:
        proactive_reservation_id = None

    await connection.execute(
        """
        INSERT INTO komari_chat_reply_commit_outbox (
            operation_id,
            payload_hash,
            request_trace_id,
            source_message_id,
            platform_message_id,
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
            proactive_confirmed_at,
            favorability_applied_at,
            ai_history_stored_at,
            interaction_stored_at,
            attempt_count,
            next_retry_at,
            lease_owner,
            lease_expires_at,
            last_error_code,
            created_at,
            delivered_at,
            completed_at,
            updated_at,
            delivery_state,
            bot_self_id,
            adapter_name,
            reply_target_message_id,
            prepared_at,
            send_started_at,
            not_delivered_at
        )
        VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
            $11, $12, $13, $14::jsonb, $15, $16, $17, $18, $19, $20,
            $21, $22, $23, $24, $25, $26, $27, $28, $29, $30,
            $31, $32, $33, $34, $35, $36, $37, $38, $39
        )
        """,
        operation_id,
        payload_hash,
        f"trace-{operation_id}",
        f"trigger-{operation_id}",
        f"platform-{operation_id}",
        "group-1",
        "user-1",
        user_nickname,
        bot_nickname,
        reply_content,
        123.5,
        2,
        favorability_reason,
        interaction_history,
        proactive_reservation_id,
        300,
        global_interaction_enabled,
        20,
        status,
        proactive_confirmed_at,
        favorability_applied_at,
        ai_history_stored_at,
        interaction_stored_at,
        attempt_count,
        next_retry_at,
        lease_owner,
        lease_expires_at,
        last_error_code,
        now - timedelta(days=2),
        delivered_at,
        completed_at,
        updated_at or now,
        delivery_state,
        "bot-1",
        "onebot.v11",
        f"trigger-{operation_id}",
        now - timedelta(days=2),
        send_started_at,
        not_delivered_at,
    )


async def _cleanup_rows(
    connection: asyncpg.Connection,
    operation_ids: list[str],
) -> None:
    await connection.execute(
        """
        DELETE FROM komari_chat_reply_fulfillment_commitments
        WHERE fulfillment_id = ANY($1::text[])
        """,
        operation_ids,
    )
    await connection.execute(
        """
        DELETE FROM komari_chat_reply_fulfillments
        WHERE fulfillment_id = ANY($1::text[])
        """,
        operation_ids,
    )
    await connection.execute(
        """
        DELETE FROM komari_chat_reply_commit_outbox
        WHERE operation_id = ANY($1::text[])
        """,
        operation_ids,
    )


async def test_backfill_maps_six_legacy_states_and_is_repeatable() -> None:
    """六种旧状态原子转换，冻结指纹原样继承且终态只留最小身份。"""
    if not _same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot 数据库配置不一致")

    result = _run_bootstrap("upgrade", "0010")
    assert result.returncode == 0, result.stderr
    connection = await asyncpg.connect(**_parse_dsn(POSTGRES_URL))
    operation_ids = [
        "tsk86-prepared",
        "tsk86-delivered",
        "tsk86-processing",
        "tsk86-failed",
        "tsk86-completed",
        "tsk86-cancelled",
    ]
    now = datetime.now(UTC)
    try:
        await _require_empty_reply_tables(connection)
        result = _run_bootstrap("downgrade", "0009")
        assert result.returncode == 0, result.stderr

        await _insert_legacy_row(
            connection,
            operation_id=operation_ids[0],
            payload_hash="1" * 64,
            status="PREPARED",
            delivery_state="NOT_STARTED",
            now=now,
        )
        await _insert_legacy_row(
            connection,
            operation_id=operation_ids[1],
            payload_hash="2" * 64,
            status="DELIVERED",
            delivery_state="DELIVERED",
            now=now,
            proactive_confirmed_at=now - timedelta(minutes=5),
            favorability_applied_at=now - timedelta(minutes=4),
            attempt_count=2,
            next_retry_at=now + timedelta(minutes=10),
            last_error_code="temporary_provider_error",
            send_started_at=now - timedelta(minutes=10),
            delivered_at=now - timedelta(minutes=9),
        )
        await _insert_legacy_row(
            connection,
            operation_id=operation_ids[2],
            payload_hash="3" * 64,
            status="PROCESSING",
            delivery_state="DELIVERED",
            now=now,
            proactive_reservation_id=None,
            global_interaction_enabled=False,
            proactive_confirmed_at=now - timedelta(minutes=5),
            favorability_applied_at=now - timedelta(minutes=3),
            interaction_stored_at=now - timedelta(minutes=2),
            attempt_count=3,
            lease_owner="legacy-worker",
            lease_expires_at=now + timedelta(hours=1),
            send_started_at=now - timedelta(minutes=10),
            delivered_at=now - timedelta(minutes=9),
        )
        await _insert_legacy_row(
            connection,
            operation_id=operation_ids[3],
            payload_hash="4" * 64,
            status="FAILED",
            delivery_state="DELIVERED",
            now=now,
            proactive_confirmed_at=now - timedelta(minutes=5),
            favorability_applied_at=now - timedelta(minutes=4),
            attempt_count=4,
            last_error_code="invalid_payload",
            send_started_at=now - timedelta(minutes=10),
            delivered_at=now - timedelta(minutes=9),
            updated_at=now - timedelta(days=1),
        )
        await _insert_legacy_row(
            connection,
            operation_id=operation_ids[4],
            payload_hash="a" * 64,
            status="COMPLETED",
            delivery_state="DELIVERED",
            now=now,
            proactive_confirmed_at=now - timedelta(minutes=5),
            favorability_applied_at=now - timedelta(minutes=4),
            ai_history_stored_at=now - timedelta(minutes=3),
            interaction_stored_at=now - timedelta(minutes=2),
            completed_at=now - timedelta(minutes=1),
            send_started_at=now - timedelta(minutes=10),
            delivered_at=now - timedelta(minutes=9),
            terminal_payload_minimized=True,
        )
        await _insert_legacy_row(
            connection,
            operation_id=operation_ids[5],
            payload_hash="b" * 64,
            status="CANCELLED",
            delivery_state="NOT_DELIVERED",
            now=now,
            send_started_at=None,
            not_delivered_at=now - timedelta(minutes=1),
        )

        result = _run_bootstrap("upgrade", "0010")
        assert result.returncode == 0, result.stderr

        parents = await connection.fetch(
            """
            SELECT fulfillment_id, payload_hash, delivery_state, reply_content,
                   lease_owner, lease_expires_at, completed_at,
                   pending_confirmation_alerted_at
            FROM komari_chat_reply_fulfillments
            WHERE fulfillment_id = ANY($1::text[])
            ORDER BY fulfillment_id
            """,
            operation_ids,
        )
        assert len(parents) == 6
        by_id = {row["fulfillment_id"]: row for row in parents}
        assert {
            operation_id: by_id[operation_id]["payload_hash"]
            for operation_id in operation_ids
        } == {
            operation_ids[0]: "1" * 64,
            operation_ids[1]: "2" * 64,
            operation_ids[2]: "3" * 64,
            operation_ids[3]: "4" * 64,
            operation_ids[4]: "a" * 64,
            operation_ids[5]: "b" * 64,
        }
        assert by_id[operation_ids[0]]["delivery_state"] == "PENDING_CONFIRMATION"
        assert by_id[operation_ids[0]]["reply_content"] == (
            f"回复正文-{operation_ids[0]}"
        )
        assert by_id[operation_ids[1]]["delivery_state"] == "DELIVERED"
        assert by_id[operation_ids[2]]["delivery_state"] == "DELIVERED"
        assert by_id[operation_ids[3]]["delivery_state"] == "DELIVERED"
        assert by_id[operation_ids[4]]["completed_at"] is not None
        assert by_id[operation_ids[5]]["delivery_state"] == "NOT_DELIVERED"
        assert all(row["lease_owner"] is None for row in parents)
        assert all(row["lease_expires_at"] is None for row in parents)
        assert all(row["pending_confirmation_alerted_at"] is None for row in parents)
        assert all(
            row["reply_content"] is None
            for operation_id, row in by_id.items()
            if operation_id != operation_ids[0]
        )

        children = await connection.fetch(
            """
            SELECT fulfillment_id, commitment_type, state, attempt_count,
                   next_retry_at, last_error_code, payload, completed_at,
                   disposition_alerted_at
            FROM komari_chat_reply_fulfillment_commitments
            WHERE fulfillment_id = ANY($1::text[])
            ORDER BY fulfillment_id, commitment_type
            """,
            operation_ids,
        )
        children_by_parent: dict[str, dict[str, asyncpg.Record]] = {}
        for row in children:
            children_by_parent.setdefault(row["fulfillment_id"], {})[
                row["commitment_type"]
            ] = row

        assert set(children_by_parent[operation_ids[0]]) == {
            "proactive_reply_confirmation",
            "favorability_adjustment",
            "assistant_reply_history",
            "interaction_history",
        }
        assert all(
            row["state"] == "PENDING"
            for row in children_by_parent[operation_ids[0]].values()
        )
        assert all(
            row["payload"] is not None
            for row in children_by_parent[operation_ids[0]].values()
        )

        delivered_children = children_by_parent[operation_ids[1]]
        assert delivered_children["proactive_reply_confirmation"]["state"] == (
            "COMPLETED"
        )
        assert delivered_children["favorability_adjustment"]["state"] == "COMPLETED"
        assert delivered_children["assistant_reply_history"]["state"] == "RETRY_WAIT"
        assert delivered_children["assistant_reply_history"]["attempt_count"] == 2
        assert (
            delivered_children["assistant_reply_history"]["next_retry_at"] is not None
        )
        assert delivered_children["assistant_reply_history"]["last_error_code"] == (
            "temporary_provider_error"
        )
        assert delivered_children["interaction_history"]["state"] == "PENDING"
        assert delivered_children["interaction_history"]["attempt_count"] == 0

        processing_children = children_by_parent[operation_ids[2]]
        assert set(processing_children) == {
            "favorability_adjustment",
            "assistant_reply_history",
        }
        assert processing_children["favorability_adjustment"]["state"] == "COMPLETED"
        assert processing_children["assistant_reply_history"]["state"] == "PENDING"
        assert processing_children["assistant_reply_history"]["attempt_count"] == 3

        failed_children = children_by_parent[operation_ids[3]]
        assert failed_children["proactive_reply_confirmation"]["state"] == "COMPLETED"
        assert failed_children["favorability_adjustment"]["state"] == "COMPLETED"
        assert failed_children["assistant_reply_history"]["state"] == "FAILED"
        assert failed_children["assistant_reply_history"]["attempt_count"] == 4
        assert failed_children["assistant_reply_history"]["last_error_code"] == (
            "invalid_payload"
        )
        assert failed_children["assistant_reply_history"]["payload"] is not None
        assert failed_children["interaction_history"]["state"] == "PENDING"
        assert failed_children["interaction_history"]["attempt_count"] == 0
        assert all(row["disposition_alerted_at"] is None for row in children)

        assert operation_ids[4] not in children_by_parent
        assert operation_ids[5] not in children_by_parent
        assert all(
            row["payload"] is None for row in children if row["state"] == "COMPLETED"
        )

        before_repeat = [dict(row) for row in parents] + [dict(row) for row in children]
        result = _run_bootstrap("upgrade", "0010")
        assert result.returncode == 0, result.stderr
        repeated_parents = await connection.fetch(
            """
            SELECT fulfillment_id, payload_hash, delivery_state, reply_content,
                   lease_owner, lease_expires_at, completed_at,
                   pending_confirmation_alerted_at
            FROM komari_chat_reply_fulfillments
            WHERE fulfillment_id = ANY($1::text[])
            ORDER BY fulfillment_id
            """,
            operation_ids,
        )
        repeated_children = await connection.fetch(
            """
            SELECT fulfillment_id, commitment_type, state, attempt_count,
                   next_retry_at, last_error_code, payload, completed_at,
                   disposition_alerted_at
            FROM komari_chat_reply_fulfillment_commitments
            WHERE fulfillment_id = ANY($1::text[])
            ORDER BY fulfillment_id, commitment_type
            """,
            operation_ids,
        )
        assert [dict(row) for row in repeated_parents] + [
            dict(row) for row in repeated_children
        ] == before_repeat
    finally:
        await _cleanup_rows(connection, operation_ids)
        result = _run_bootstrap("upgrade", "0010")
        assert result.returncode == 0, result.stderr
        await connection.close()


async def test_ambiguous_failed_history_aborts_before_any_backfill() -> None:
    """超过原 31 天幂等证据窗口的 FAILED 使整个 revision 回滚。"""
    if not _same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot 数据库配置不一致")

    result = _run_bootstrap("upgrade", "0010")
    assert result.returncode == 0, result.stderr
    connection = await asyncpg.connect(**_parse_dsn(POSTGRES_URL))
    operation_ids = ["tsk86-safe-prepared", "tsk86-ambiguous-failed"]
    now = datetime.now(UTC)
    sensitive_reply = "不得出现在迁移错误里的正文"
    try:
        await _require_empty_reply_tables(connection)
        result = _run_bootstrap("downgrade", "0009")
        assert result.returncode == 0, result.stderr
        await _insert_legacy_row(
            connection,
            operation_id=operation_ids[0],
            payload_hash="5" * 64,
            status="PREPARED",
            delivery_state="NOT_STARTED",
            now=now,
        )
        await _insert_legacy_row(
            connection,
            operation_id=operation_ids[1],
            payload_hash="6" * 64,
            status="FAILED",
            delivery_state="DELIVERED",
            now=now,
            attempt_count=20,
            last_error_code="exhausted",
            send_started_at=now - timedelta(days=33),
            delivered_at=now - timedelta(days=33),
            updated_at=now - timedelta(days=32),
        )
        await connection.execute(
            """
            UPDATE komari_chat_reply_commit_outbox
            SET reply_content = $2
            WHERE operation_id = $1
            """,
            operation_ids[1],
            sensitive_reply,
        )

        result = _run_bootstrap("upgrade", "0010")
        assert result.returncode != 0
        output = f"{result.stdout}\n{result.stderr}"
        assert "ambiguous_failed_count=1" in output
        assert f"minimum_fulfillment_id={operation_ids[1]}" in output
        assert sensitive_reply not in output

        assert (
            await connection.fetchval("SELECT version_num FROM alembic_version")
            == "0009"
        )
        assert (
            await connection.fetchval(
                """
            SELECT COUNT(*)
            FROM komari_chat_reply_fulfillments
            WHERE fulfillment_id = ANY($1::text[])
            """,
                operation_ids,
            )
            == 0
        )
        assert (
            await connection.fetchval(
                """
            SELECT COUNT(*)
            FROM komari_chat_reply_fulfillment_commitments
            WHERE fulfillment_id = ANY($1::text[])
            """,
                operation_ids,
            )
            == 0
        )
        assert (
            await connection.fetchval(
                """
            SELECT COUNT(*)
            FROM komari_chat_reply_commit_outbox
            WHERE operation_id = ANY($1::text[])
            """,
                operation_ids,
            )
            == 2
        )
    finally:
        await _cleanup_rows(connection, operation_ids)
        result = _run_bootstrap("upgrade", "0010")
        assert result.returncode == 0, result.stderr
        await connection.close()
