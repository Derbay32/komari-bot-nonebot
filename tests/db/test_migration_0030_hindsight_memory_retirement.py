"""TSK-197: revision 0030 retires the old memory surfaces without touching sources."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

import asyncpg
import pytest

from komari_bot.plugins.komari_context.config_schema import (
    KomariContextConfigSchema,
)
from komari_bot.plugins.komari_memory.config_schema import KomariMemoryConfigSchema
from tests.config.chat_prompt_field_contract import HISTORICAL_CHAT_FIELDS
from tests.db.tsk197_gate_support import (
    HEAD_REVISION,
    SKIP_NO_POSTGRES,
    drop_scratch_database,
    recreate_scratch_database,
    run_bootstrap,
    scratch_url,
)

pytestmark = [SKIP_NO_POSTGRES, pytest.mark.asyncio]

MEMORY_TABLE = "komari_memory_config"
CONTEXT_TABLE = "komari_context_config"
CHAT_PROMPT_TABLE = "komari_prompt_komari_chat"
RETIRED_PROMPT_TABLE = "komari_prompt_memory_summary"
DOWNGRADE_CODE = "0030_TSK197_IS_IRREVERSIBLE"

#: 33 legacy-only memory columns removed by 0030.
RETIRED_MEMORY_FIELDS: frozenset[str] = frozenset(
    {
        "llm_model_summary",
        "llm_temperature_summary",
        "llm_max_tokens_summary",
        "llm_request_api_summary",
        "llm_stream_enabled_summary",
        "llm_thinking_mode_summary",
        "llm_reasoning_effort_summary",
        "summary_idle_timeout",
        "summary_min_messages",
        "summary_max_buffer_size",
        "conversation_snapshot_ttl_seconds",
        "conversation_processing_lease_seconds",
        "profile_snapshot_ttl_seconds",
        "profile_snapshot_enable",
        "profile_trait_limit",
        "memory_agent_max_rounds",
        "memory_agent_staging_ttl_seconds",
        "memory_agent_max_tool_calls",
        "memory_agent_max_read_profiles",
        "memory_agent_max_write_operations",
        "memory_agent_lock_timeout_seconds",
        "global_interaction_summary_interval_minutes",
        "global_interaction_processing_lease_seconds",
        "response_tag",
        "forgetting_enabled",
        "forgetting_importance_threshold",
        "forgetting_decay_factor",
        "forgetting_access_boost",
        "forgetting_min_age_days",
        "forgetting_fuzzify_concurrency",
        "forgetting_job_lease_seconds",
        "query_rewrite_history_limit",
        "bot_aliases",
    }
)

#: Two interaction-buffer columns removed from the context resource.
RETIRED_CONTEXT_FIELDS: frozenset[str] = frozenset(
    {"global_interaction_enabled", "global_interaction_trigger_size"}
)

#: The dead chat prompt column removed by 0030 (its prose is not merged).
RETIRED_CHAT_PROMPT_FIELDS: frozenset[str] = frozenset({"profile_read_instruction"})

INTERNAL_FIELDS = {"id", "revision", "updated_at"}

MEMORY_SURVIVING_VALUES: dict[str, Any] = {
    "plugin_enable": True,
    "hindsight_base_url": "https://hindsight.invalid",
    "hindsight_api_key": "local-test-key-not-real",
    "hindsight_bank_id": "retirement-bank",
    "hindsight_request_timeout_seconds": 7.5,
    "hindsight_recovery_interval_seconds": 45.0,
}
MEMORY_REVISION = 77
CONTEXT_REVISION = 12
CHAT_PROMPT_REVISION = 9
UPDATED_AT = datetime(2026, 10, 25, 20, 30, tzinfo=UTC)


async def _column_names(connection: asyncpg.Connection, table: str) -> set[str]:
    rows = await connection.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name = $1",
        table,
    )
    return {str(row["column_name"]) for row in rows}


def _default_for(  # noqa: PLR0911
    data_type: str, *, nullable: bool, column: str
) -> Any:
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
        return UPDATED_AT
    if column == "llm_request_api_summary":
        return "chat_completions"
    return ""


async def _insert_config_row(
    connection: asyncpg.Connection,
    *,
    table: str,
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
            values[column] = UPDATED_AT
        elif column in overrides:
            values[column] = overrides[column]
        else:
            values[column] = _default_for(
                str(row["data_type"]),
                nullable=str(row["is_nullable"]) == "YES",
                column=column,
            )
    columns = list(values)
    placeholders = ", ".join(f"${index}" for index in range(1, len(columns) + 1))
    assignments = ", ".join(
        f"{column} = EXCLUDED.{column}" for column in columns if column != "id"
    )
    await connection.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT (id) DO UPDATE SET {assignments}",
        *(values[column] for column in columns),
    )


async def _insert_chat_prompt_row(connection: asyncpg.Connection) -> None:
    rows = await connection.fetch(
        "SELECT column_name, data_type, is_nullable "
        "FROM information_schema.columns WHERE table_name = $1 "
        "ORDER BY ordinal_position",
        CHAT_PROMPT_TABLE,
    )
    values: dict[str, Any] = {}
    for row in rows:
        column = str(row["column_name"])
        if column == "id":
            values[column] = 1
        elif column == "revision":
            values[column] = CHAT_PROMPT_REVISION
        elif column == "updated_at":
            values[column] = UPDATED_AT
        elif column == "profile_read_instruction":
            # Retired prose must never be merged into another field.
            values[column] = "RETIRED-PROFILE-MARKER-tsk197"
        else:
            values[column] = f"admin-{column}"
    columns = list(values)
    placeholders = ", ".join(f"${index}" for index in range(1, len(columns) + 1))
    await connection.execute(
        f"INSERT INTO {CHAT_PROMPT_TABLE} ({', '.join(columns)}) VALUES ({placeholders})",
        *(values[column] for column in columns),
    )


_FORMAL_SOURCE_TABLES = (
    "komari_memory_conversations",
    "komari_memory_user_profile",
    "komari_memory_interaction_history",
    "komari_memory_conversation_embeddings",
    "komari_memory_jobs",
)

_SOURCE_INDEX_PATTERN = "komari_memory_%"


async def _vector_dimension(
    connection: asyncpg.Connection,
    table: str,
) -> int:
    """Read the existing vector dimension instead of hardcoding one."""
    dimension = await connection.fetchval(
        "SELECT atttypmod FROM pg_attribute "
        "WHERE attrelid = $1::regclass AND attname = 'embedding' "
        "AND NOT attisdropped",
        table,
    )
    assert isinstance(dimension, int) and dimension > 0
    return dimension


async def _insert_formal_source_rows(connection: asyncpg.Connection) -> None:
    conversation_id = await connection.fetchval(
        "INSERT INTO komari_memory_conversations "
        "(group_id, summary, participants, dedup_key, start_time, end_time, "
        " importance, importance_initial, importance_current, last_accessed, "
        " is_fuzzy, created_at) "
        "VALUES ('source-group', 'formal source canary', "
        "        ARRAY['alice','bob'], 'source-dedup-1', "
        "        '2026-10-20 10:00:00', '2026-10-20 10:30:00', "
        "        4, 5, 3, '2026-10-21 09:00:00', TRUE, "
        "        '2026-10-20 10:31:00') "
        "RETURNING id"
    )
    await connection.execute(
        "INSERT INTO komari_memory_user_profile "
        "(user_id, group_id, version, display_name, traits, updated_at, "
        " importance, access_count, last_accessed) "
        "VALUES ('source-user', 'source-group', 3, '来源用户', $1::jsonb, "
        "        $2, 5, 17, '2026-10-21 09:30:00')",
        '{"identity":"长期身份","preferences":["咖啡","旧书"],"confidence":0.9}',
        UPDATED_AT,
    )
    await connection.execute(
        "INSERT INTO komari_memory_interaction_history "
        "(user_id, display_name, event_summary, source_dedup_key, "
        " source_message_count, first_seen_at, last_seen_at, importance, "
        " importance_initial, importance_current, last_accessed, is_fuzzy, "
        " created_at) "
        "VALUES ('source-user', '来源用户', 'formal interaction canary', "
        "        'interaction-dedup-1', 7, '2026-10-20 11:00:00+00', "
        "        '2026-10-20 11:05:00+00', 4, 5, 2, "
        "        '2026-10-21 09:45:00+00', TRUE, "
        "        '2026-10-20 11:06:00+00')"
    )
    dimension = await _vector_dimension(
        connection, "komari_memory_conversation_embeddings"
    )
    vector = "[" + ",".join("0.01" for _ in range(dimension)) + "]"
    await connection.execute(
        "INSERT INTO komari_memory_conversation_embeddings "
        "(conversation_id, content_hash, embedding, embedding_dim, embedded_at) "
        "VALUES ($1, $2, $3::vector, $4, '2026-10-20 10:32:00+00')",
        conversation_id,
        hashlib.sha256(b"formal source canary").hexdigest(),
        vector,
        dimension,
    )
    await connection.execute(
        "INSERT INTO komari_memory_jobs "
        "(job_name, run_date, owner_token, lease_until, stage, attempt, "
        " last_error_code, started_at, updated_at, completed_at) "
        "VALUES ('forgetting', '2026-10-20', 'source-owner', "
        "        '2026-10-20 12:00:00+00', 'completed', 3, "
        "        'historical_safe_code', '2026-10-20 11:00:00+00', "
        "        '2026-10-20 11:10:00+00', '2026-10-20 11:11:00+00')"
    )


async def _source_rows(
    connection: asyncpg.Connection,
) -> dict[str, list[dict[str, Any]]]:
    snapshot: dict[str, list[dict[str, Any]]] = {}
    for table in _FORMAL_SOURCE_TABLES:
        order = "user_id, group_id" if table == "komari_memory_user_profile" else "id"
        if table == "komari_memory_jobs":
            order = "job_name, run_date"
        rows = await connection.fetch(f"SELECT * FROM {table} ORDER BY {order}")
        snapshot[table] = [dict(row) for row in rows]
    return snapshot


async def _source_index_definitions(
    connection: asyncpg.Connection,
) -> list[dict[str, Any]]:
    rows = await connection.fetch(
        "SELECT indexname, indexdef FROM pg_indexes "
        "WHERE schemaname = 'public' AND tablename LIKE $1 "
        "ORDER BY indexname",
        _SOURCE_INDEX_PATTERN,
    )
    return [dict(row) for row in rows]


async def _assert_formal_source_snapshot(
    connection: asyncpg.Connection,
    before_rows: dict[str, list[dict[str, Any]]],
    before_indexes: list[dict[str, Any]],
) -> None:
    assert await _source_rows(connection) == before_rows
    assert await _source_index_definitions(connection) == before_indexes
    for table in _FORMAL_SOURCE_TABLES:
        assert (
            await connection.fetchval("SELECT to_regclass($1)::text", table) is not None
        ), f"formal source storage {table} must survive 0030"


async def test_0030_drops_retired_surface_and_preserves_sources_and_admin_values() -> (
    None
):
    scratch = await recreate_scratch_database("_tsk197_retirement")
    db_url = scratch_url(str(scratch["database"]))
    try:
        result = run_bootstrap(db_url, "upgrade", "0029")
        assert result.returncode == 0, result.stderr

        connection = await asyncpg.connect(**scratch)
        try:
            await _insert_config_row(
                connection,
                table=MEMORY_TABLE,
                revision=MEMORY_REVISION,
                overrides=MEMORY_SURVIVING_VALUES,
            )
            await _insert_config_row(
                connection,
                table=CONTEXT_TABLE,
                revision=CONTEXT_REVISION,
                overrides={
                    "redis_db": 7,
                    "bot_nickname": "迁移后昵称",
                    "error_notify_enabled": False,
                    "global_interaction_enabled": False,
                    "global_interaction_trigger_size": 77,
                },
            )
            await _insert_chat_prompt_row(connection)
            await _insert_formal_source_rows(connection)
            source_rows_before = await _source_rows(connection)
            source_indexes_before = await _source_index_definitions(connection)
        finally:
            await connection.close()

        result = run_bootstrap(db_url, "upgrade", "0030")
        assert result.returncode == 0, result.stderr

        connection = await asyncpg.connect(**scratch)
        try:
            assert (
                await connection.fetchval("SELECT version_num FROM alembic_version")
                == "0030"
            )

            memory_columns = await _column_names(connection, MEMORY_TABLE)
            # 0030 边界的历史形状：0035 才退役 plugin_enable，本用例显式固定
            # 当时的存活列集合，不用当前模型反推历史结构。
            expected_memory = INTERNAL_FIELDS | set(MEMORY_SURVIVING_VALUES)
            assert memory_columns == expected_memory
            assert not RETIRED_MEMORY_FIELDS & memory_columns
            memory_row = dict(
                await connection.fetchrow(f"SELECT * FROM {MEMORY_TABLE} WHERE id = 1")
            )
            for field, expected in MEMORY_SURVIVING_VALUES.items():
                assert memory_row[field] == expected, field
            assert memory_row["revision"] == MEMORY_REVISION
            assert memory_row["updated_at"] == UPDATED_AT

            context_columns = await _column_names(connection, CONTEXT_TABLE)
            expected_context = set(KomariContextConfigSchema.model_fields)
            assert context_columns == expected_context
            assert not RETIRED_CONTEXT_FIELDS & context_columns
            context_row = dict(
                await connection.fetchrow(f"SELECT * FROM {CONTEXT_TABLE} WHERE id = 1")
            )
            assert context_row["redis_db"] == 7
            assert context_row["bot_nickname"] == "迁移后昵称"
            assert context_row["error_notify_enabled"] is False
            assert context_row["revision"] == CONTEXT_REVISION
            assert context_row["updated_at"] == UPDATED_AT

            prompt_columns = await _column_names(connection, CHAT_PROMPT_TABLE)
            expected_prompt = set(HISTORICAL_CHAT_FIELDS) | {"id", "revision", "updated_at"}
            assert prompt_columns == expected_prompt
            assert not RETIRED_CHAT_PROMPT_FIELDS & prompt_columns
            prompt_row = dict(
                await connection.fetchrow(
                    f"SELECT * FROM {CHAT_PROMPT_TABLE} WHERE id = 1"
                )
            )
            for field in expected_prompt - INTERNAL_FIELDS:
                assert prompt_row[field] == f"admin-{field}", field
            assert prompt_row["revision"] == CHAT_PROMPT_REVISION
            assert prompt_row["updated_at"] == UPDATED_AT

            assert (
                await connection.fetchval(
                    "SELECT to_regclass($1)::text", RETIRED_PROMPT_TABLE
                )
                is None
            )
            await _assert_formal_source_snapshot(
                connection, source_rows_before, source_indexes_before
            )
        finally:
            await connection.close()

        downgrade = run_bootstrap(db_url, "downgrade", "0029")
        assert downgrade.returncode != 0
        combined = f"{downgrade.stdout}\n{downgrade.stderr}"
        assert DOWNGRADE_CODE in combined
        connection = await asyncpg.connect(**scratch)
        try:
            assert (
                await connection.fetchval("SELECT version_num FROM alembic_version")
                == "0030"
            )
            assert RETIRED_PROMPT_TABLE not in {
                str(row["tablename"])
                for row in await connection.fetch(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                )
            }
        finally:
            await connection.close()

        # The historical 0030 guard is verified above.  Continue to the current
        # pre-retirement revision and prove no source row or index changes.
        result = run_bootstrap(db_url, "upgrade", "0032")
        assert result.returncode == 0, result.stderr
        connection = await asyncpg.connect(**scratch)
        try:
            assert (
                await connection.fetchval("SELECT version_num FROM alembic_version")
                == "0032"
            )
            await _assert_formal_source_snapshot(
                connection, source_rows_before, source_indexes_before
            )
            assert (
                dict(
                    await connection.fetchrow(
                        f"SELECT * FROM {MEMORY_TABLE} WHERE id = 1"
                    )
                )
                == memory_row
            )
            assert (
                dict(
                    await connection.fetchrow(
                        f"SELECT * FROM {CONTEXT_TABLE} WHERE id = 1"
                    )
                )
                == context_row
            )
            assert (
                dict(
                    await connection.fetchrow(
                        f"SELECT * FROM {CHAT_PROMPT_TABLE} WHERE id = 1"
                    )
                )
                == prompt_row
            )
        finally:
            await connection.close()
    finally:
        await drop_scratch_database(str(scratch["database"]))


async def test_0030_fresh_database_and_legacy_upgrade_keep_hindsight_configuration() -> (
    None
):
    fresh = await recreate_scratch_database("_tsk197_retirement_fresh")
    fresh_url = scratch_url(str(fresh["database"]))
    try:
        result = run_bootstrap(fresh_url, "upgrade", "head")
        assert result.returncode == 0, result.stderr
        check = run_bootstrap(fresh_url, "check")
        assert check.returncode == 0, f"{check.stdout}\n{check.stderr}"
        connection = await asyncpg.connect(**fresh)
        try:
            assert (
                await connection.fetchval("SELECT version_num FROM alembic_version")
                == HEAD_REVISION
            )
            assert await _column_names(connection, MEMORY_TABLE) == set(
                KomariMemoryConfigSchema.model_fields
            )
            assert await _column_names(connection, CONTEXT_TABLE) == set(
                KomariContextConfigSchema.model_fields
            )
            assert (
                await connection.fetchval(
                    "SELECT to_regclass($1)::text", RETIRED_PROMPT_TABLE
                )
                is None
            )
        finally:
            await connection.close()
    finally:
        await drop_scratch_database(str(fresh["database"]))

    legacy = await recreate_scratch_database("_tsk197_retirement_legacy")
    legacy_url = scratch_url(str(legacy["database"]))
    try:
        result = run_bootstrap(legacy_url, "upgrade", "0011")
        assert result.returncode == 0, result.stderr
        connection = await asyncpg.connect(**legacy)
        try:
            rows = await connection.fetch(
                "SELECT column_name, data_type, is_nullable "
                "FROM information_schema.columns WHERE table_name = $1",
                MEMORY_TABLE,
            )
            values: dict[str, Any] = {}
            for row in rows:
                column = str(row["column_name"])
                if column == "id":
                    values[column] = 1
                elif column == "revision":
                    values[column] = 55
                elif column == "updated_at":
                    values[column] = UPDATED_AT
                elif column == "plugin_enable":
                    values[column] = True
                elif column == "llm_model_summary":
                    values[column] = "legacy-summary-model"
                else:
                    values[column] = _default_for(
                        str(row["data_type"]),
                        nullable=str(row["is_nullable"]) == "YES",
                        column=column,
                    )
            columns = list(values)
            placeholders = ", ".join(
                f"${index}" for index in range(1, len(columns) + 1)
            )
            await connection.execute(
                f"INSERT INTO {MEMORY_TABLE} ({', '.join(columns)}) "
                f"VALUES ({placeholders})",
                *(values[column] for column in columns),
            )
        finally:
            await connection.close()

        result = run_bootstrap(legacy_url, "upgrade", "head")
        assert result.returncode == 0, result.stderr
        connection = await asyncpg.connect(**legacy)
        try:
            row = dict(
                await connection.fetchrow(f"SELECT * FROM {MEMORY_TABLE} WHERE id = 1")
            )
            # 0035 已删除停用开关；其余历史值与修订元数据必须原样保留。
            assert "plugin_enable" not in row
            assert row["hindsight_base_url"] == ""
            assert row["hindsight_request_timeout_seconds"] == 10.0
            assert row["revision"] == 55
            assert row["updated_at"] == UPDATED_AT
        finally:
            await connection.close()
    finally:
        await drop_scratch_database(str(legacy["database"]))
