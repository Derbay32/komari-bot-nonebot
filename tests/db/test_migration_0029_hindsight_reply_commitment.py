"""TSK-195 revision 0029: Hindsight reply commitment cutover guard.

Every case runs inside a disposable database derived from the authorized
gate DSN. The refusal cases prove that an old unfinished fulfillment aborts
the protocol change before any source row is modified; the success cases
prove completed and not-delivered history survives the upgrade unchanged.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import asyncpg
import pytest

from tests.db.tsk197_gate_support import (
    SKIP_NO_POSTGRES,
    drop_scratch_database,
    parse_dsn,
    recreate_scratch_database,
    run_bootstrap,
    scratch_url,
)

pytestmark = [SKIP_NO_POSTGRES, pytest.mark.asyncio]

HEAD = "0030"
PREVIOUS = "0028"
UNFINISHED_CODE = "TSK195_UNFINISHED_REPLY_FULFILLMENT"
PARENT = "komari_chat_reply_fulfillments"
CHILD = "komari_chat_reply_fulfillment_commitments"
_NOW = datetime(2026, 10, 25, 20, 30, tzinfo=UTC)
_HISTORICAL_PAYLOAD = {
    "user_id": "user-1",
    "display_name": "测试用户",
    "trigger_size": 20,
    "reply_timestamp": 2.0,
    "trigger_message_id": "message-1",
    "record": {"event": "发言", "result": "回复", "emotion": "平静"},
}


def _upgrade(database: str, revision: str) -> None:
    result = run_bootstrap(scratch_url(database), "upgrade", revision)
    assert result.returncode == 0, result.stderr


async def _insert_parent(
    connection: asyncpg.Connection,
    fulfillment_id: str,
    *,
    delivery_state: str,
    completed: bool = False,
) -> None:
    sent = delivery_state in {"PENDING_CONFIRMATION", "DELIVERED", "NOT_DELIVERED"}
    delivered = delivery_state == "DELIVERED"
    not_delivered = delivery_state == "NOT_DELIVERED"
    await connection.execute(
        f"""
        INSERT INTO {PARENT} (
            fulfillment_id, payload_hash, request_trace_id,
            trigger_message_id, trigger_user_id, group_id, bot_self_id,
            adapter_name, reply_target_message_id, reply_content,
            delivery_state, send_started_at, delivered_at, not_delivered_at,
            completed_at
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
            $11, $12, $13, $14, $15
        )
        """,
        fulfillment_id,
        "a" * 64,
        f"trace-{fulfillment_id}",
        "message-1",
        "user-1",
        "group-1",
        "bot-1",
        "onebot.v11",
        "message-1",
        "回复正文",
        delivery_state,
        _NOW if sent else None,
        _NOW if delivered else None,
        _NOW if not_delivered else None,
        _NOW if completed else None,
    )


async def _insert_child(
    connection: asyncpg.Connection,
    fulfillment_id: str,
    *,
    commitment_type: str = "interaction_history",
    state: str = "PENDING",
    payload: dict[str, object] | None = None,
) -> None:
    await connection.execute(
        f"""
        INSERT INTO {CHILD} (
            fulfillment_id, commitment_type, state, payload,
            completed_at
        ) VALUES ($1, $2, $3, $4::jsonb, $5)
        """,
        fulfillment_id,
        commitment_type,
        state,
        None if payload is None else json.dumps(payload, ensure_ascii=False),
        _NOW if state == "COMPLETED" else None,
    )


async def _snapshot(database: str) -> dict[str, object]:
    connection = await asyncpg.connect(**parse_dsn(scratch_url(database)))
    try:
        parents = await connection.fetch(
            f"SELECT * FROM {PARENT} ORDER BY fulfillment_id"
        )
        children = await connection.fetch(
            f"SELECT * FROM {CHILD} ORDER BY fulfillment_id, commitment_type"
        )
        version = await connection.fetchval("SELECT version_num FROM alembic_version")
        definition = await connection.fetchval(
            """
            SELECT pg_get_constraintdef(con.oid)
            FROM pg_constraint AS con
            JOIN pg_class AS rel ON rel.oid = con.conrelid
            WHERE rel.relname = $1 AND con.contype = 'c'
              AND pg_get_constraintdef(con.oid) LIKE '%interaction_history%'
            ORDER BY con.conname
            LIMIT 1
            """,
            CHILD,
        )
    finally:
        await connection.close()
    return {
        "parents": [dict(row) for row in parents],
        "children": [dict(row) for row in children],
        "version": version,
        "definition": definition,
    }


@pytest.mark.parametrize(
    "delivery_state",
    ["NOT_STARTED", "PENDING_CONFIRMATION", "DELIVERED"],
)
async def test_0029_refuses_unfinished_parent_and_preserves_source_rows(
    delivery_state: str,
) -> None:
    scratch = await recreate_scratch_database(f"_tsk195_guard_{delivery_state}")
    database = str(scratch["database"])
    try:
        _upgrade(database, PREVIOUS)
        fulfillment_id = f"reply-unfinished-{delivery_state.lower()}"
        connection = await asyncpg.connect(**scratch)
        try:
            await _insert_parent(
                connection,
                fulfillment_id,
                delivery_state=delivery_state,
            )
            await _insert_child(connection, fulfillment_id)
        finally:
            await connection.close()

        before = await _snapshot(database)
        result = run_bootstrap(scratch_url(database), "upgrade", "head")

        assert result.returncode != 0
        assert UNFINISHED_CODE in result.stderr
        assert fulfillment_id not in result.stderr
        assert await _snapshot(database) == before
    finally:
        await drop_scratch_database(database)


async def test_0029_upgrades_empty_database_to_head_with_zero_orm_drift() -> None:
    scratch = await recreate_scratch_database("_tsk195_empty")
    database = str(scratch["database"])
    try:
        _upgrade(database, PREVIOUS)
        result = run_bootstrap(scratch_url(database), "upgrade", "0029")

        assert result.returncode == 0, result.stderr
        connection = await asyncpg.connect(**scratch)
        try:
            version = await connection.fetchval(
                "SELECT version_num FROM alembic_version"
            )
        finally:
            await connection.close()
        assert version == "0029"
        # Historical invariant holds at 0029; the current head must still be
        # reachable with zero ORM drift.
        head_result = run_bootstrap(scratch_url(database), "upgrade", "head")
        assert head_result.returncode == 0, head_result.stderr
        connection = await asyncpg.connect(**scratch)
        try:
            assert (
                await connection.fetchval(
                    "SELECT version_num FROM alembic_version"
                )
                == HEAD
            )
        finally:
            await connection.close()
        check = run_bootstrap(scratch_url(database), "check")
        assert check.returncode == 0, f"{check.stdout}\n{check.stderr}"
    finally:
        await drop_scratch_database(database)


async def test_0029_preserves_terminal_history_and_allows_new_commitment() -> None:
    scratch = await recreate_scratch_database("_tsk195_terminal")
    database = str(scratch["database"])
    try:
        _upgrade(database, PREVIOUS)
        connection = await asyncpg.connect(**scratch)
        try:
            completed_parent = "reply-completed"
            await _insert_parent(
                connection,
                completed_parent,
                delivery_state="DELIVERED",
                completed=True,
            )
            await _insert_child(
                connection,
                completed_parent,
                state="COMPLETED",
                payload=None,
            )
            not_delivered_parent = "reply-not-delivered"
            await _insert_parent(
                connection,
                not_delivered_parent,
                delivery_state="NOT_DELIVERED",
            )
            await _insert_child(
                connection,
                not_delivered_parent,
                state="PENDING",
                payload=None,
            )
        finally:
            await connection.close()

        before = await _snapshot(database)
        result = run_bootstrap(scratch_url(database), "upgrade", "0029")
        assert result.returncode == 0, result.stderr
        after = await _snapshot(database)

        assert after["version"] == "0029"
        assert after["parents"] == before["parents"]
        assert after["children"] == before["children"]
        assert after["definition"] != before["definition"]
        assert "hindsight_memory" in str(after["definition"])

        connection = await asyncpg.connect(**scratch)
        try:
            constraint_parent = "reply-constraint-probes"
            await _insert_parent(
                connection,
                constraint_parent,
                delivery_state="DELIVERED",
                completed=True,
            )
            # The historical literal stays usable only as a payload-free fact.
            with pytest.raises(asyncpg.CheckViolationError):
                await _insert_child(
                    connection,
                    constraint_parent,
                    state="PENDING",
                    payload=_HISTORICAL_PAYLOAD,
                )
            await _insert_child(
                connection,
                constraint_parent,
                state="PENDING",
                payload=None,
            )
            await _insert_child(
                connection,
                constraint_parent,
                commitment_type="hindsight_memory",
                state="PENDING",
                payload={"request": "{}"},
            )
            with pytest.raises(asyncpg.CheckViolationError):
                await _insert_child(
                    connection,
                    constraint_parent,
                    commitment_type="unknown_commitment",
                    state="PENDING",
                )
        finally:
            await connection.close()
    finally:
        await drop_scratch_database(database)


async def test_0029_refuses_downgrade_without_restoring_old_protocol() -> None:
    scratch = await recreate_scratch_database("_tsk195_downgrade")
    database = str(scratch["database"])
    try:
        _upgrade(database, PREVIOUS)
        _upgrade(database, "0029")
        before = await _snapshot(database)

        result = run_bootstrap(scratch_url(database), "downgrade", PREVIOUS)

        assert result.returncode != 0
        assert "0029_TSK195_IS_IRREVERSIBLE" in result.stderr
        assert await _snapshot(database) == before
    finally:
        await drop_scratch_database(database)
