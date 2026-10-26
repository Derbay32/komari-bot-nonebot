"""TSK-192 phase B: chat/context configuration ownership migration."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import asyncpg
import pytest

from komari_bot.plugins.komari_chat.config_schema import KomariChatConfigSchema
from komari_bot.plugins.komari_context.config_schema import (
    KomariContextConfigSchema,
)
from tests.db.tsk197_gate_support import (
    SKIP_NO_POSTGRES,
    drop_scratch_database,
    recreate_scratch_database,
    run_bootstrap,
    scratch_url,
)

pytestmark = [SKIP_NO_POSTGRES, pytest.mark.asyncio]

_MEMORY_TABLE = "komari_memory_config"
_CHAT_TABLE = "komari_chat_config"
_CONTEXT_TABLE = "komari_context_config"
_PROMPT_TABLE = "komari_prompt_komari_chat"
_SOURCE_REVISION = 77
_CHAT_REVISION = 42
_CONTEXT_REVISION = 12
_UPDATED_AT = datetime(2026, 10, 25, 18, 45, tzinfo=UTC)

#: 0031 retires this dead chat field.  Historical 0027/0026 assertions keep
#: requiring its exact moved value; only the current-head view omits it.
RETIRED_CHAT_FIELDS: frozenset[str] = frozenset({"memory_search_limit"})

MOVED_TO_CHAT: dict[str, Any] = {
    "llm_model_chat": "chat-model-phase-b",
    "llm_temperature_chat": 0.42,
    "llm_max_tokens_chat": 1234,
    "llm_request_api_chat": "responses",
    "llm_stream_enabled_chat": True,
    "llm_thinking_mode_chat": True,
    "llm_reasoning_effort_chat": "high",
    "assistant_prefill_enabled": True,
    "dsv4_roleplay_instruct_mode": "inner_os",
    "knowledge_enabled": False,
    "knowledge_limit": 7,
    "context_messages_limit": 33,
    "context_max_utf8_bytes": 12_345,
    "context_max_estimated_tokens": 3_456,
    "face_reaction_enabled": True,
    "face_reaction_id": "",
    "memory_search_limit": 8,
}

MOVED_TO_CONTEXT: dict[str, Any] = {
    "bot_nickname": "小鞠迁移验证",
    "error_notify_enabled": False,
    "global_interaction_enabled": False,
    "global_interaction_trigger_size": 77,
}

QUERY_REWRITE_SOURCE: dict[str, Any] = {
    "llm_model_summary": "summary-model-phase-b",
    "llm_request_api_summary": "responses",
    "llm_stream_enabled_summary": True,
    "llm_thinking_mode_summary": True,
    "llm_reasoning_effort_summary": "",
}

QUERY_REWRITE_TARGET: dict[str, Any] = {
    "query_rewrite_model": "summary-model-phase-b",
    "query_rewrite_request_api": "responses",
    "query_rewrite_stream_enabled": True,
    "query_rewrite_thinking_mode": True,
    "query_rewrite_reasoning_effort": "",
}

CHAT_SENTINELS: dict[str, Any] = {
    "proactive_cooldown": 777,
    "reply_fulfillment_max_attempts": 33,
    "agent_max_rounds": 11,
    "agent_max_tool_calls_per_round": 5,
    "agent_max_total_tool_calls": 22,
    "image_understanding_mode": "native",
    "vision_image_download_max_count": 6,
}

MEMORY_RETAINED_SENTINELS: dict[str, Any] = {
    "plugin_enable": True,
    "response_tag": "phase_b_content",
    "bot_aliases": json.dumps(["phase-b", "小鞠"]),
    "query_rewrite_history_limit": 9,
}


def _default_for_type(  # noqa: PLR0911
    column: str,
    data_type: str,
    *,
    nullable: bool,
) -> Any:
    if column == "reply_fulfillment_freshness_seconds":
        return 120
    if nullable:
        return None
    if data_type == "boolean":
        return False
    if data_type == "jsonb":
        return "{}"
    if data_type in {"integer", "bigint"}:
        return 1
    if data_type in {"real", "double precision"}:
        return 1.0
    if data_type.startswith("timestamp"):
        return _UPDATED_AT
    return ""


async def _insert_single_row(
    connection: asyncpg.Connection,
    table: str,
    *,
    revision: int,
    overrides: dict[str, Any],
) -> None:
    rows = await connection.fetch(
        "SELECT column_name, data_type, is_nullable "
        "FROM information_schema.columns WHERE table_name = $1 "
        "ORDER BY ordinal_position",
        table,
    )
    values: dict[str, Any] = {}
    for row in rows:
        column = str(row["column_name"])
        if column == "id":
            values[column] = 1
        elif column == "revision":
            values[column] = revision
        elif column == "updated_at":
            values[column] = _UPDATED_AT
        elif column in overrides:
            values[column] = overrides[column]
        else:
            values[column] = _default_for_type(
                column,
                str(row["data_type"]),
                nullable=str(row["is_nullable"]) == "YES",
            )
    columns = list(values)
    placeholders = ", ".join(f"${index}" for index in range(1, len(columns) + 1))
    await connection.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
        *(values[column] for column in columns),
    )


async def _fetch_row(connection: asyncpg.Connection, table: str) -> dict[str, Any]:
    row = await connection.fetchrow(f"SELECT * FROM {table} WHERE id = 1")
    assert row is not None
    return dict(row)


async def _assert_prompt_row_unchanged(
    connection: asyncpg.Connection,
    before: dict[str, Any],
    *,
    retired_fields: frozenset[str] = frozenset(),
) -> None:
    """Require exact preservation, with only explicitly retired columns removed."""
    after = await _fetch_row(connection, _PROMPT_TABLE)
    expected = {
        key: value for key, value in before.items() if key not in retired_fields
    }
    assert after == expected


async def _insert_prompt_row(connection: asyncpg.Connection) -> None:
    """Insert a chat prompt row without running the current seed CLI at 0026."""
    rows = await connection.fetch(
        "SELECT column_name, data_type, is_nullable "
        "FROM information_schema.columns WHERE table_name = $1 "
        "ORDER BY ordinal_position",
        _PROMPT_TABLE,
    )
    values: dict[str, Any] = {}
    for row in rows:
        column = str(row["column_name"])
        if column == "id":
            values[column] = 1
        elif column == "revision":
            values[column] = 5
        elif column == "updated_at":
            values[column] = _UPDATED_AT
        elif str(row["is_nullable"]) == "YES":
            values[column] = None
        else:
            values[column] = f"admin-{column}"
    columns = list(values)
    placeholders = ", ".join(f"${index}" for index in range(1, len(columns) + 1))
    await connection.execute(
        f"INSERT INTO {_PROMPT_TABLE} ({', '.join(columns)}) "
        f"VALUES ({placeholders}) ON CONFLICT (id) DO NOTHING",
        *(values[column] for column in columns),
    )


async def test_migration_moves_every_owned_field_and_preserves_unrelated_data() -> None:
    scratch = await recreate_scratch_database("_tsk192b_values")
    db_url = scratch_url(str(scratch["database"]))
    try:
        result = run_bootstrap(db_url, "upgrade", "0026")
        assert result.returncode == 0, result.stderr

        connection = await asyncpg.connect(**scratch)
        try:
            await _insert_single_row(
                connection,
                _MEMORY_TABLE,
                revision=_SOURCE_REVISION,
                overrides={
                    **MOVED_TO_CHAT,
                    **MOVED_TO_CONTEXT,
                    **QUERY_REWRITE_SOURCE,
                    **MEMORY_RETAINED_SENTINELS,
                },
            )
            await _insert_single_row(
                connection,
                _CHAT_TABLE,
                revision=_CHAT_REVISION,
                overrides=CHAT_SENTINELS,
            )
            await _insert_single_row(
                connection,
                _CONTEXT_TABLE,
                revision=_CONTEXT_REVISION,
                overrides={"redis_db": 6},
            )
            await connection.execute(
                "INSERT INTO komari_memory_conversations "
                "(group_id, summary, start_time, end_time) "
                "VALUES ('g1', 'formal memory canary', now(), now())"
            )
            await _insert_prompt_row(connection)
            prompt_before = await _fetch_row(connection, _PROMPT_TABLE)
        finally:
            await connection.close()

        # Historical invariant at 0027's intended revision.
        result = run_bootstrap(db_url, "upgrade", "0027")
        assert result.returncode == 0, result.stderr
        connection = await asyncpg.connect(**scratch)
        try:
            chat = await _fetch_row(connection, _CHAT_TABLE)
            for field, expected in {**MOVED_TO_CHAT, **QUERY_REWRITE_TARGET}.items():
                assert chat[field] == expected, field
            for field, expected in CHAT_SENTINELS.items():
                assert chat[field] == expected, field
            assert chat["plugin_enable"] is True
            assert chat["revision"] == _CHAT_REVISION
            assert chat["updated_at"] == _UPDATED_AT

            context = await _fetch_row(connection, _CONTEXT_TABLE)
            for field, expected in MOVED_TO_CONTEXT.items():
                assert context[field] == expected, field
            assert context["redis_db"] == 6
            assert context["revision"] == _CONTEXT_REVISION
            assert context["updated_at"] == _UPDATED_AT

            memory = await _fetch_row(connection, _MEMORY_TABLE)
            for field in {*MOVED_TO_CHAT, *MOVED_TO_CONTEXT}:
                assert field not in memory, field
            for field, expected in {
                **QUERY_REWRITE_SOURCE,
                **MEMORY_RETAINED_SENTINELS,
            }.items():
                if field == "bot_aliases":
                    continue
                assert memory[field] == expected, field
            assert json.loads(memory["bot_aliases"]) == ["phase-b", "小鞠"]
            assert memory["revision"] == _SOURCE_REVISION
            assert (
                await connection.fetchval(
                    "SELECT summary FROM komari_memory_conversations WHERE group_id = 'g1'"
                )
                == "formal memory canary"
            )
            await _assert_prompt_row_unchanged(connection, prompt_before)
        finally:
            await connection.close()

        downgraded = run_bootstrap(db_url, "downgrade", "0026")
        assert downgraded.returncode == 0, downgraded.stderr
        connection = await asyncpg.connect(**scratch)
        try:
            memory = await _fetch_row(connection, _MEMORY_TABLE)
            for field, expected in MOVED_TO_CHAT.items():
                assert memory[field] == expected, field
            for field, expected in MOVED_TO_CONTEXT.items():
                assert memory[field] == expected, field
            assert json.loads(memory["bot_aliases"]) == ["phase-b", "小鞠"]
            assert (
                await connection.fetchval(
                    "SELECT summary FROM komari_memory_conversations WHERE group_id = 'g1'"
                )
                == "formal memory canary"
            )
            await _assert_prompt_row_unchanged(connection, prompt_before)
            chat_columns = {
                str(row["column_name"])
                for row in await connection.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = $1",
                    _CHAT_TABLE,
                )
            }
            context_columns = {
                str(row["column_name"])
                for row in await connection.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = $1",
                    _CONTEXT_TABLE,
                )
            }
            assert not (set(MOVED_TO_CHAT) | set(QUERY_REWRITE_TARGET)) & chat_columns
            assert not set(MOVED_TO_CONTEXT) & context_columns
        finally:
            await connection.close()

        # Advance to the current head: 0030 removes only the retired memory
        # columns plus 0031 the dead chat memory_search_limit, while surviving
        # chat/context live values and admin prompts stay intact.
        result = run_bootstrap(db_url, "upgrade", "head")
        assert result.returncode == 0, result.stderr
        check = run_bootstrap(db_url, "check")
        assert check.returncode == 0, f"{check.stdout}\n{check.stderr}"
        connection = await asyncpg.connect(**scratch)
        try:
            chat = await _fetch_row(connection, _CHAT_TABLE)
            for field, expected in {**MOVED_TO_CHAT, **QUERY_REWRITE_TARGET}.items():
                if field in RETIRED_CHAT_FIELDS:
                    continue
                assert chat[field] == expected, field
            for field in RETIRED_CHAT_FIELDS:
                assert field not in chat, field
            for field, expected in CHAT_SENTINELS.items():
                assert chat[field] == expected, field
            assert chat["plugin_enable"] is True
            assert chat["revision"] == _CHAT_REVISION
            assert chat["updated_at"] == _UPDATED_AT

            context = await _fetch_row(connection, _CONTEXT_TABLE)
            for field in ("bot_nickname", "error_notify_enabled"):
                assert context[field] == MOVED_TO_CONTEXT[field], field
            assert context["redis_db"] == 6
            assert context["revision"] == _CONTEXT_REVISION
            assert context["updated_at"] == _UPDATED_AT
            assert not {
                "global_interaction_enabled",
                "global_interaction_trigger_size",
            } & set(context)

            memory = await _fetch_row(connection, _MEMORY_TABLE)
            assert memory["plugin_enable"] is True
            assert memory["revision"] == _SOURCE_REVISION
            assert memory["updated_at"] == _UPDATED_AT
            retired_fields = {
                *MOVED_TO_CHAT,
                *QUERY_REWRITE_SOURCE,
                *MEMORY_RETAINED_SENTINELS,
                "bot_aliases",
            } - {"plugin_enable"}
            for field in retired_fields:
                assert field not in memory, field
            assert (
                await connection.fetchval(
                    "SELECT summary FROM komari_memory_conversations WHERE group_id = 'g1'"
                )
                == "formal memory canary"
            )
            await _assert_prompt_row_unchanged(
                connection,
                prompt_before,
                retired_fields=frozenset({"profile_read_instruction"}),
            )
        finally:
            await connection.close()
    finally:
        await drop_scratch_database(str(scratch["database"]))


async def test_migration_creates_missing_rows_with_schema_defaults() -> None:
    scratch = await recreate_scratch_database("_tsk192b_defaults")
    db_url = scratch_url(str(scratch["database"]))
    try:
        result = run_bootstrap(db_url, "upgrade", "0026")
        assert result.returncode == 0, result.stderr

        result = run_bootstrap(db_url, "upgrade", "head")
        assert result.returncode == 0, result.stderr

        connection = await asyncpg.connect(**scratch)
        try:
            chat = await _fetch_row(connection, _CHAT_TABLE)
            expected_chat = KomariChatConfigSchema().model_dump(mode="json")
            assert "memory_search_limit" not in chat
            for field, expected in expected_chat.items():
                assert chat[field] == expected, field
            assert chat["revision"] == 1

            context = await _fetch_row(connection, _CONTEXT_TABLE)
            expected_context = KomariContextConfigSchema().model_dump(mode="json")
            for field, expected in expected_context.items():
                assert context[field] == expected, field
            assert context["revision"] == 1
        finally:
            await connection.close()
    finally:
        await drop_scratch_database(str(scratch["database"]))


async def test_migration_preserves_existing_target_rows_without_source_row() -> None:
    scratch = await recreate_scratch_database("_tsk192b_targets")
    db_url = scratch_url(str(scratch["database"]))
    try:
        result = run_bootstrap(db_url, "upgrade", "0026")
        assert result.returncode == 0, result.stderr

        connection = await asyncpg.connect(**scratch)
        try:
            await _insert_single_row(
                connection,
                _CHAT_TABLE,
                revision=_CHAT_REVISION,
                overrides=CHAT_SENTINELS,
            )
            await _insert_single_row(
                connection,
                _CONTEXT_TABLE,
                revision=_CONTEXT_REVISION,
                overrides={"redis_db": 9},
            )
        finally:
            await connection.close()

        result = run_bootstrap(db_url, "upgrade", "head")
        assert result.returncode == 0, result.stderr

        connection = await asyncpg.connect(**scratch)
        try:
            chat = await _fetch_row(connection, _CHAT_TABLE)
            assert "memory_search_limit" not in chat
            for field, expected in CHAT_SENTINELS.items():
                assert chat[field] == expected, field
            assert chat["plugin_enable"] is False
            assert chat["llm_model_chat"] == (
                KomariChatConfigSchema.model_fields["llm_model_chat"].default
            )
            assert chat["revision"] == _CHAT_REVISION

            context = await _fetch_row(connection, _CONTEXT_TABLE)
            assert context["redis_db"] == 9
            assert context["bot_nickname"] == (
                KomariContextConfigSchema.model_fields["bot_nickname"].default
            )
            assert context["revision"] == _CONTEXT_REVISION
        finally:
            await connection.close()
    finally:
        await drop_scratch_database(str(scratch["database"]))


async def test_migration_uses_source_row_when_both_target_rows_are_missing() -> None:
    """Execute the source-backed INSERT branch for both target singletons."""
    assert len(MOVED_TO_CHAT) == 17
    assert len(QUERY_REWRITE_TARGET) == 5
    assert len(MOVED_TO_CONTEXT) == 4

    scratch = await recreate_scratch_database("_tsk192b_source_insert")
    db_url = scratch_url(str(scratch["database"]))
    try:
        result = run_bootstrap(db_url, "upgrade", "0026")
        assert result.returncode == 0, result.stderr

        connection = await asyncpg.connect(**scratch)
        try:
            await _insert_single_row(
                connection,
                _MEMORY_TABLE,
                revision=_SOURCE_REVISION,
                overrides={
                    **MOVED_TO_CHAT,
                    **MOVED_TO_CONTEXT,
                    **QUERY_REWRITE_SOURCE,
                    **MEMORY_RETAINED_SENTINELS,
                },
            )
            await connection.execute(
                "INSERT INTO komari_memory_conversations "
                "(group_id, summary, start_time, end_time) "
                "VALUES ('g1', 'source-only memory canary', now(), now())"
            )
            await _insert_prompt_row(connection)
            prompt_before = await _fetch_row(connection, _PROMPT_TABLE)
            assert (
                await connection.fetchval(
                    f"SELECT EXISTS (SELECT 1 FROM {_CHAT_TABLE} WHERE id = 1)"
                )
                is False
            )
            assert (
                await connection.fetchval(
                    f"SELECT EXISTS (SELECT 1 FROM {_CONTEXT_TABLE} WHERE id = 1)"
                )
                is False
            )
        finally:
            await connection.close()

        # Historical invariant at 0027's intended revision (source-backed
        # INSERT branch for both target singletons).
        result = run_bootstrap(db_url, "upgrade", "0027")
        assert result.returncode == 0, result.stderr
        connection = await asyncpg.connect(**scratch)
        try:
            chat = await _fetch_row(connection, _CHAT_TABLE)
            for field, expected in MOVED_TO_CHAT.items():
                assert chat[field] == expected, field
            for field, expected in QUERY_REWRITE_TARGET.items():
                assert chat[field] == expected, field
            assert chat["plugin_enable"] is True
            assert chat["revision"] == _SOURCE_REVISION
            assert chat["updated_at"] == _UPDATED_AT

            context = await _fetch_row(connection, _CONTEXT_TABLE)
            for field, expected in MOVED_TO_CONTEXT.items():
                assert context[field] == expected, field
            assert context["redis_db"] == 1
            assert context["revision"] == 1

            memory = await _fetch_row(connection, _MEMORY_TABLE)
            for field in {*MOVED_TO_CHAT, *MOVED_TO_CONTEXT}:
                assert field not in memory, field
            for field, expected in {
                **QUERY_REWRITE_SOURCE,
                **MEMORY_RETAINED_SENTINELS,
            }.items():
                if field == "bot_aliases":
                    continue
                assert memory[field] == expected, field
            assert json.loads(memory["bot_aliases"]) == ["phase-b", "小鞠"]
            assert memory["revision"] == _SOURCE_REVISION
            await _assert_prompt_row_unchanged(connection, prompt_before)
        finally:
            await connection.close()

        result = run_bootstrap(db_url, "upgrade", "head")
        assert result.returncode == 0, result.stderr
        check = run_bootstrap(db_url, "check")
        assert check.returncode == 0, f"{check.stdout}\n{check.stderr}"

        connection = await asyncpg.connect(**scratch)
        try:
            chat = await _fetch_row(connection, _CHAT_TABLE)
            chat_defaults = KomariChatConfigSchema().model_dump(mode="json")
            moved_chat_fields = {
                *MOVED_TO_CHAT,
                *QUERY_REWRITE_TARGET,
                "plugin_enable",
            }
            for field, expected in MOVED_TO_CHAT.items():
                if field in RETIRED_CHAT_FIELDS:
                    continue
                assert chat[field] == expected, field
            for field in RETIRED_CHAT_FIELDS:
                assert field not in chat, field
            for field, expected in QUERY_REWRITE_TARGET.items():
                assert chat[field] == expected, field
            assert chat["plugin_enable"] is True
            for field, expected in chat_defaults.items():
                if field not in moved_chat_fields:
                    assert chat[field] == expected, field
            assert chat["revision"] == _SOURCE_REVISION
            assert chat["updated_at"] == _UPDATED_AT

            context = await _fetch_row(connection, _CONTEXT_TABLE)
            for field in ("bot_nickname", "error_notify_enabled"):
                assert context[field] == MOVED_TO_CONTEXT[field], field
            assert context["redis_db"] == 1
            assert context["revision"] == 1
            assert isinstance(context["updated_at"], datetime)
            assert context["updated_at"].tzinfo is not None
            assert not {
                "global_interaction_enabled",
                "global_interaction_trigger_size",
            } & set(context)

            memory = await _fetch_row(connection, _MEMORY_TABLE)
            assert memory["plugin_enable"] is True
            assert memory["revision"] == _SOURCE_REVISION
            retired_fields = {
                *MOVED_TO_CHAT,
                *MOVED_TO_CONTEXT,
                *QUERY_REWRITE_SOURCE,
                *MEMORY_RETAINED_SENTINELS,
                "bot_aliases",
            } - {"plugin_enable"}
            for field in retired_fields:
                assert field not in memory, field
            assert (
                await connection.fetchval(
                    "SELECT summary FROM komari_memory_conversations WHERE group_id = 'g1'"
                )
                == "source-only memory canary"
            )
            await _assert_prompt_row_unchanged(
                connection,
                prompt_before,
                retired_fields=frozenset({"profile_read_instruction"}),
            )
        finally:
            await connection.close()
    finally:
        await drop_scratch_database(str(scratch["database"]))
