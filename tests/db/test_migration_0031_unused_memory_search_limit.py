"""TSK-191 review rework B: revision 0031 retires the unused chat setting.

The chat ``memory_search_limit`` field has no caller after the Hindsight
cutover.  Revision 0031 removes exactly that dead column, preserves every
surviving chat value/revision/timestamp, and refuses a downgrade with a
stable forward-repair code.
"""

from __future__ import annotations

import importlib.util
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from komari_bot.plugins.komari_chat.config_schema import KomariChatConfigSchema
from tests.db.tsk197_gate_support import (
    HEAD_REVISION,
    SKIP_NO_POSTGRES,
    drop_scratch_database,
    recreate_scratch_database,
    run_bootstrap,
    scratch_url,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = (
    PROJECT_ROOT / "migrations" / "versions" / "0031_unused_memory_search_limit.py"
)
CHAT_TABLE = "komari_chat_config"
RETIRED_COLUMN = "memory_search_limit"
CHAT_REVISION = 4242
UPDATED_AT = datetime(2026, 10, 26, 1, 45, tzinfo=UTC)
DOWNGRADE_CODE = "0031_TSK197_IS_IRREVERSIBLE"

#: Non-default values that must survive the one-column drop exactly.
SURVIVING_VALUES: dict[str, Any] = {
    "memory_search_limit": 9,
    "plugin_enable": True,
    "llm_model_chat": "tsk-0031-chat-model",
    "llm_request_api_chat": "responses",
    "knowledge_limit": 7,
    "context_messages_limit": 33,
    "face_reaction_enabled": True,
    "face_reaction_id": "",
    "proactive_cooldown": 777,
    "reply_fulfillment_max_attempts": 33,
    "agent_max_rounds": 12,
    "agent_max_tool_calls_per_round": 5,
    "agent_max_total_tool_calls": 30,
    "image_understanding_mode": "native",
    "vision_image_download_max_count": 6,
    "revision": CHAT_REVISION,
    "updated_at": UPDATED_AT,
}


def _load_migration_module() -> Any:
    spec = importlib.util.spec_from_file_location("mig_0031_guard", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0031_source_is_narrow_column_drop_with_irreversible_guard() -> None:
    """0031 exists only as the one-column drop plus the refusal guard."""
    assert MIGRATION_PATH.name == "0031_unused_memory_search_limit.py"
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    module = _load_migration_module()

    assert module.revision == "0031"
    assert module.down_revision == "0030"
    assert DOWNGRADE_CODE in source
    assert CHAT_TABLE in source

    drops = re.findall(r"drop_column\(\s*[\"']([^\"']+)[\"']", source)
    assert drops == [RETIRED_COLUMN]

    # No archive/marker table, no other DDL and no runtime dependency.
    for forbidden in (
        "create_table",
        "add_column",
        "alter_column",
        "drop_table",
        "create_index",
        "drop_index",
        "execute(",
        "bulk_insert",
        "from komari_bot",
        "import komari_bot",
    ):
        assert forbidden not in source, forbidden

    # Named non-default migration binds remain a no-op, exactly like 0030.
    module.upgrade("test-named-bind")
    module.downgrade("test-named-bind")
    with pytest.raises(RuntimeError, match=DOWNGRADE_CODE):
        module.downgrade()


async def _chat_row(connection: asyncpg.Connection) -> dict[str, Any]:
    row = await connection.fetchrow(f"SELECT * FROM {CHAT_TABLE} WHERE id = 1")
    assert row is not None
    return dict(row)


async def _chat_columns(connection: asyncpg.Connection) -> set[str]:
    rows = await connection.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name = $1",
        CHAT_TABLE,
    )
    return {str(row["column_name"]) for row in rows}


@SKIP_NO_POSTGRES
async def test_0031_drops_only_dead_setting_and_preserves_surviving_chat_data() -> None:
    scratch = await recreate_scratch_database("_tsk191_0031_drop")
    db_url = scratch_url(str(scratch["database"]))
    try:
        result = run_bootstrap(db_url, "upgrade", "0030")
        assert result.returncode == 0, result.stderr

        connection = await asyncpg.connect(**scratch)
        try:
            exists = await connection.fetchval(
                f"SELECT EXISTS (SELECT 1 FROM {CHAT_TABLE} WHERE id = 1)"
            )
            assert exists is True, "0027 must keep creating the chat singleton"
            assignments = ", ".join(
                f"{column} = ${index}"
                for index, column in enumerate(SURVIVING_VALUES, start=1)
            )
            await connection.execute(
                f"UPDATE {CHAT_TABLE} SET {assignments} WHERE id = 1",
                *SURVIVING_VALUES.values(),
            )
            before = await _chat_row(connection)
            assert before[RETIRED_COLUMN] == 9
            for field, expected in SURVIVING_VALUES.items():
                assert before[field] == expected, field
            assert before["revision"] == CHAT_REVISION
            assert before["updated_at"] == UPDATED_AT
        finally:
            await connection.close()

        result = run_bootstrap(db_url, "upgrade", "head")
        assert result.returncode == 0, result.stderr

        connection = await asyncpg.connect(**scratch)
        try:
            assert (
                await connection.fetchval("SELECT version_num FROM alembic_version")
                == HEAD_REVISION
            )
            columns = await _chat_columns(connection)
            assert RETIRED_COLUMN not in columns
            assert columns == set(KomariChatConfigSchema.model_fields)
            after = await _chat_row(connection)
            expected_after = {
                key: value for key, value in before.items() if key != RETIRED_COLUMN
            }
            assert after == expected_after
        finally:
            await connection.close()

        check = run_bootstrap(db_url, "check")
        assert check.returncode == 0, f"{check.stdout}\n{check.stderr}"

        # TSK-200：链在 0032 先拒绝降级，head 上的降级不再到达 0031。
        downgrade = run_bootstrap(db_url, "downgrade", "0030")
        assert downgrade.returncode != 0
        latest_code = "0032_TSK200_IS_IRREVERSIBLE"
        assert latest_code in f"{downgrade.stdout}\n{downgrade.stderr}"
        connection = await asyncpg.connect(**scratch)
        try:
            assert (
                await connection.fetchval("SELECT version_num FROM alembic_version")
                == HEAD_REVISION
            )
            assert RETIRED_COLUMN not in await _chat_columns(connection)
            assert await _chat_row(connection) == expected_after
        finally:
            await connection.close()
    finally:
        await drop_scratch_database(str(scratch["database"]))

    # 0031 自身的降级拒绝仍在真实链上生效（数据库固定在 0031 时验证）。
    guard_scratch = await recreate_scratch_database("_tsk191_0031_guard")
    guard_url = scratch_url(str(guard_scratch["database"]))
    try:
        upgraded = run_bootstrap(guard_url, "upgrade", "0031")
        assert upgraded.returncode == 0, upgraded.stderr

        blocked = run_bootstrap(guard_url, "downgrade", "0030")
        assert blocked.returncode != 0
        assert DOWNGRADE_CODE in f"{blocked.stdout}\n{blocked.stderr}"

        connection = await asyncpg.connect(**guard_scratch)
        try:
            assert (
                await connection.fetchval("SELECT version_num FROM alembic_version")
                == "0031"
            )
        finally:
            await connection.close()
    finally:
        await drop_scratch_database(str(guard_scratch["database"]))


@SKIP_NO_POSTGRES
async def test_0031_fresh_full_chain_reaches_head_without_dead_setting() -> None:
    scratch = await recreate_scratch_database("_tsk191_0031_fresh")
    db_url = scratch_url(str(scratch["database"]))
    try:
        result = run_bootstrap(db_url, "upgrade", "head")
        assert result.returncode == 0, result.stderr

        check = run_bootstrap(db_url, "check")
        assert check.returncode == 0, f"{check.stdout}\n{check.stderr}"

        connection = await asyncpg.connect(**scratch)
        try:
            assert (
                await connection.fetchval("SELECT version_num FROM alembic_version")
                == HEAD_REVISION
            )
            columns = await _chat_columns(connection)
            assert RETIRED_COLUMN not in columns
            assert columns == set(KomariChatConfigSchema.model_fields)
        finally:
            await connection.close()
    finally:
        await drop_scratch_database(str(scratch["database"]))
