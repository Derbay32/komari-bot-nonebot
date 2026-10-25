"""Chat/context configuration ownership (TSK-192 phase B).

Revision ID: 0027
Revises: 0026

Moves chat generation, prompt, reaction and query-rewrite settings to
``komari_chat_config`` and shared identity/notification/interaction policy to
``komari_context_config``.  The migration creates missing target singleton rows
with schema defaults, copies populated source values without truthiness
fallbacks, then removes only the moved columns from ``komari_memory_config``.

Administrator prompt rows and formal-memory tables are not touched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0027"
down_revision: str | Sequence[str] | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHAT_TABLE = "komari_chat_config"
_CONTEXT_TABLE = "komari_context_config"
_MEMORY_TABLE = "komari_memory_config"

#: New chat-owned columns: (name, SQL type, SQL default literal).
_CHAT_NEW_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("plugin_enable", "BOOLEAN", "false"),
    ("llm_model_chat", "VARCHAR", "'gemini-3-flash-preview'"),
    ("llm_temperature_chat", "FLOAT", "1.0"),
    ("llm_max_tokens_chat", "INTEGER", "4000"),
    ("llm_request_api_chat", "VARCHAR(32)", "'chat_completions'"),
    ("llm_stream_enabled_chat", "BOOLEAN", "false"),
    ("llm_thinking_mode_chat", "BOOLEAN", "false"),
    ("llm_reasoning_effort_chat", "VARCHAR", "''"),
    ("assistant_prefill_enabled", "BOOLEAN", "false"),
    ("dsv4_roleplay_instruct_mode", "VARCHAR", "'auto'"),
    ("knowledge_enabled", "BOOLEAN", "true"),
    ("knowledge_limit", "INTEGER", "3"),
    ("memory_search_limit", "INTEGER", "3"),
    ("context_messages_limit", "INTEGER", "10"),
    ("context_max_utf8_bytes", "INTEGER", "24000"),
    ("context_max_estimated_tokens", "INTEGER", "6000"),
    ("face_reaction_enabled", "BOOLEAN", "false"),
    ("face_reaction_id", "VARCHAR", "'76'"),
    ("query_rewrite_model", "VARCHAR", "'deepseek-v4-flash'"),
    ("query_rewrite_request_api", "VARCHAR(32)", "'chat_completions'"),
    ("query_rewrite_stream_enabled", "BOOLEAN", "false"),
    ("query_rewrite_thinking_mode", "BOOLEAN", "false"),
    ("query_rewrite_reasoning_effort", "VARCHAR", "''"),
)

_CONTEXT_NEW_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("bot_nickname", "VARCHAR", "'小鞠知花'"),
    ("error_notify_enabled", "BOOLEAN", "true"),
    ("global_interaction_enabled", "BOOLEAN", "true"),
    ("global_interaction_trigger_size", "INTEGER", "20"),
)

#: Existing chat columns and their schema defaults, used only when the chat
#: singleton row is missing at upgrade time.
_CHAT_EXISTING_DEFAULTS: tuple[tuple[str, str], ...] = (
    ("proactive_enabled", "false"),
    ("proactive_cooldown", "300"),
    ("proactive_max_per_hour", "400"),
    ("proactive_reservation_ttl_seconds", "360"),
    ("reply_fulfillment_worker_interval_seconds", "5"),
    ("reply_fulfillment_batch_size", "20"),
    ("reply_fulfillment_lease_seconds", "120"),
    ("reply_fulfillment_max_attempts", "20"),
    ("reply_fulfillment_retry_base_seconds", "5"),
    ("reply_fulfillment_retry_max_seconds", "3600"),
    ("reply_fulfillment_tombstone_retention_days", "30"),
    ("reply_fulfillment_freshness_seconds", "120"),
    ("agent_max_rounds", "10"),
    ("agent_max_tool_calls_per_round", "4"),
    ("agent_max_total_tool_calls", "20"),
    ("agent_tool_call_mode", "'required'"),
    ("image_understanding_mode", "'delegated'"),
    ("vision_image_download_max_count", "4"),
    ("vision_image_download_max_bytes", "8388608"),
    ("vision_image_download_total_max_bytes", "20971520"),
    ("vision_image_download_max_pixels", "40000000"),
    ("vision_image_download_concurrency", "2"),
    ("vision_image_download_connect_timeout_seconds", "5.0"),
    ("vision_image_download_read_timeout_seconds", "30.0"),
    ("vision_image_download_total_timeout_seconds", "45.0"),
)

#: Direct column moves from memory to chat.  Empty strings and false values
#: stay literal because the UPDATE has no COALESCE/truthiness fallback.
_MOVED_TO_CHAT: tuple[str, ...] = (
    "llm_model_chat",
    "llm_temperature_chat",
    "llm_max_tokens_chat",
    "llm_request_api_chat",
    "llm_stream_enabled_chat",
    "llm_thinking_mode_chat",
    "llm_reasoning_effort_chat",
    "assistant_prefill_enabled",
    "dsv4_roleplay_instruct_mode",
    "knowledge_enabled",
    "knowledge_limit",
    "context_messages_limit",
    "context_max_utf8_bytes",
    "context_max_estimated_tokens",
    "face_reaction_enabled",
    "face_reaction_id",
    "memory_search_limit",
)

_QUERY_REWRITE_MAP: tuple[tuple[str, str], ...] = (
    ("query_rewrite_model", "llm_model_summary"),
    ("query_rewrite_request_api", "llm_request_api_summary"),
    ("query_rewrite_stream_enabled", "llm_stream_enabled_summary"),
    ("query_rewrite_thinking_mode", "llm_thinking_mode_summary"),
    ("query_rewrite_reasoning_effort", "llm_reasoning_effort_summary"),
)

_MOVED_TO_CONTEXT: tuple[str, ...] = (
    "bot_nickname",
    "error_notify_enabled",
    "global_interaction_enabled",
    "global_interaction_trigger_size",
)

#: Schema defaults used when downgrading recreates the memory columns.
_MEMORY_COLUMN_DEFAULTS: dict[str, str] = {
    "llm_model_chat": "'gemini-3-flash-preview'",
    "llm_temperature_chat": "1.0",
    "llm_max_tokens_chat": "4000",
    "llm_request_api_chat": "'chat_completions'",
    "llm_stream_enabled_chat": "false",
    "llm_thinking_mode_chat": "false",
    "llm_reasoning_effort_chat": "''",
    "assistant_prefill_enabled": "false",
    "dsv4_roleplay_instruct_mode": "'auto'",
    "knowledge_enabled": "true",
    "knowledge_limit": "3",
    "memory_search_limit": "3",
    "context_messages_limit": "10",
    "context_max_utf8_bytes": "24000",
    "context_max_estimated_tokens": "6000",
    "face_reaction_enabled": "false",
    "face_reaction_id": "'76'",
    "bot_nickname": "'小鞠知花'",
    "error_notify_enabled": "true",
    "global_interaction_enabled": "true",
    "global_interaction_trigger_size": "20",
}

_MEMORY_COLUMN_TYPES: dict[str, str] = {
    "llm_model_chat": "VARCHAR",
    "llm_temperature_chat": "FLOAT",
    "llm_max_tokens_chat": "INTEGER",
    "llm_request_api_chat": "VARCHAR(32)",
    "llm_stream_enabled_chat": "BOOLEAN",
    "llm_thinking_mode_chat": "BOOLEAN",
    "llm_reasoning_effort_chat": "VARCHAR",
    "assistant_prefill_enabled": "BOOLEAN",
    "dsv4_roleplay_instruct_mode": "VARCHAR",
    "knowledge_enabled": "BOOLEAN",
    "knowledge_limit": "INTEGER",
    "memory_search_limit": "INTEGER",
    "context_messages_limit": "INTEGER",
    "context_max_utf8_bytes": "INTEGER",
    "context_max_estimated_tokens": "INTEGER",
    "face_reaction_enabled": "BOOLEAN",
    "face_reaction_id": "VARCHAR",
    "bot_nickname": "VARCHAR",
    "error_notify_enabled": "BOOLEAN",
    "global_interaction_enabled": "BOOLEAN",
    "global_interaction_trigger_size": "INTEGER",
}


def _add_columns(
    table: str,
    columns: tuple[tuple[str, str, str], ...],
) -> None:
    for name, column_type, default in columns:
        op.execute(
            f"ALTER TABLE {table} ADD COLUMN {name} "
            f"{column_type} NOT NULL DEFAULT {default}"
        )


def _insert_missing_targets() -> None:
    existing_columns = [name for name, _default in _CHAT_EXISTING_DEFAULTS]
    existing_values = ", ".join(default for _name, default in _CHAT_EXISTING_DEFAULTS)
    # A populated memory singleton supplies the CAS identity/timestamp; an empty
    # source gets a fresh schema-default singleton.
    op.execute(
        f"INSERT INTO {_CHAT_TABLE} (id, revision, updated_at, "
        f"{', '.join(existing_columns)}) "
        f"SELECT 1, m.revision, m.updated_at, {existing_values} "
        f"FROM {_MEMORY_TABLE} m WHERE m.id = 1 "
        f"AND NOT EXISTS (SELECT 1 FROM {_CHAT_TABLE} WHERE id = 1)"
    )
    op.execute(
        f"INSERT INTO {_CHAT_TABLE} (id, revision, updated_at, "
        f"{', '.join(existing_columns)}) "
        f"SELECT 1, 1, now(), {existing_values} "
        f"WHERE NOT EXISTS (SELECT 1 FROM {_CHAT_TABLE} WHERE id = 1)"
    )

    # Redis ownership moved to context in 0026. Phase B only supplies a
    # schema-default row when 0026 had no source row to derive it from.
    op.execute(
        f"INSERT INTO {_CONTEXT_TABLE} (id, revision, updated_at, redis_db) "
        f"SELECT 1, 1, now(), 1 "
        f"WHERE NOT EXISTS (SELECT 1 FROM {_CONTEXT_TABLE} WHERE id = 1)"
    )


def _copy_from_memory() -> None:
    chat_assignments = [f"{column} = m.{column}" for column in _MOVED_TO_CHAT]
    chat_assignments.extend(
        f"{target} = m.{source}" for target, source in _QUERY_REWRITE_MAP
    )
    chat_assignments.append("plugin_enable = m.plugin_enable")
    op.execute(
        f"UPDATE {_CHAT_TABLE} c SET {', '.join(chat_assignments)} "
        f"FROM {_MEMORY_TABLE} m WHERE c.id = 1 AND m.id = 1"
    )

    context_assignments = ", ".join(
        f"{column} = m.{column}" for column in _MOVED_TO_CONTEXT
    )
    op.execute(
        f"UPDATE {_CONTEXT_TABLE} c SET {context_assignments} "
        f"FROM {_MEMORY_TABLE} m WHERE c.id = 1 AND m.id = 1"
    )


def upgrade(name: str = "") -> None:
    if name:
        return

    _add_columns(_CHAT_TABLE, _CHAT_NEW_COLUMNS)
    _add_columns(_CONTEXT_TABLE, _CONTEXT_NEW_COLUMNS)
    _insert_missing_targets()
    _copy_from_memory()

    for column in (*_MOVED_TO_CHAT, *_MOVED_TO_CONTEXT):
        op.execute(f"ALTER TABLE {_MEMORY_TABLE} DROP COLUMN {column}")


def downgrade(name: str = "") -> None:
    if name:
        return

    for column, default in _MEMORY_COLUMN_DEFAULTS.items():
        op.execute(
            f"ALTER TABLE {_MEMORY_TABLE} ADD COLUMN {column} "
            f"{_MEMORY_COLUMN_TYPES[column]} NOT NULL DEFAULT {default}"
        )

    chat_assignments = [f"{column} = c.{column}" for column in _MOVED_TO_CHAT]
    op.execute(
        f"UPDATE {_MEMORY_TABLE} m SET {', '.join(chat_assignments)} "
        f"FROM {_CHAT_TABLE} c WHERE m.id = 1 AND c.id = 1"
    )
    context_assignments = ", ".join(
        f"{column} = c.{column}" for column in _MOVED_TO_CONTEXT
    )
    op.execute(
        f"UPDATE {_MEMORY_TABLE} m SET {context_assignments} "
        f"FROM {_CONTEXT_TABLE} c WHERE m.id = 1 AND c.id = 1"
    )

    for column in _MEMORY_COLUMN_DEFAULTS:
        op.execute(f"ALTER TABLE {_MEMORY_TABLE} ALTER COLUMN {column} DROP DEFAULT")
    for column, _column_type, _default in _CONTEXT_NEW_COLUMNS:
        op.execute(f"ALTER TABLE {_CONTEXT_TABLE} DROP COLUMN {column}")
    for column, _column_type, _default in _CHAT_NEW_COLUMNS:
        op.execute(f"ALTER TABLE {_CHAT_TABLE} DROP COLUMN {column}")
