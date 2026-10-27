"""离线历史转换器只退役本次授权资源，保留既有记忆总结映射。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg

from tests.db.tsk197_gate_support import (
    SKIP_NO_POSTGRES,
    drop_scratch_database,
    recreate_scratch_database,
    scratch_url,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/migrate_legacy_config_to_typed_tables.py"
PROMPT_FIELDS = (
    "memory_summary_common_system",
    "profile_agent_workflow_system",
    "summary_workflow_system",
    "json_response_example",
)
SUMMARY_VALUES = {
    "llm_model_summary": "historical-summary-model-207",
    "llm_temperature_summary": 0.37,
    "llm_max_tokens_summary": 1741,
    "llm_request_api_summary": "responses",
    "llm_stream_enabled_summary": True,
    "llm_thinking_mode_summary": True,
    "llm_reasoning_effort_summary": "historical-effort-207",
}
# Frozen historical fixture names, not derived from the production registry.
MEMORY_OTHER_COLUMNS = ["plugin_enable", "user_whitelist", "group_whitelist", "redis_db", "knowledge_enabled", "knowledge_limit", "summary_idle_timeout", "summary_min_messages", "summary_max_buffer_size", "conversation_snapshot_ttl_seconds", "conversation_processing_lease_seconds", "profile_snapshot_ttl_seconds", "profile_snapshot_enable", "profile_trait_limit", "memory_agent_max_rounds", "memory_agent_staging_ttl_seconds", "memory_agent_max_tool_calls", "memory_agent_max_read_profiles", "memory_agent_max_write_operations", "memory_agent_lock_timeout_seconds", "memory_search_limit", "context_messages_limit", "context_max_utf8_bytes", "context_max_estimated_tokens", "global_interaction_enabled", "global_interaction_trigger_size", "global_interaction_summary_interval_minutes", "global_interaction_processing_lease_seconds", "bot_nickname", "response_tag", "forgetting_enabled", "forgetting_importance_threshold", "forgetting_decay_factor", "forgetting_access_boost", "forgetting_min_age_days", "forgetting_fuzzify_concurrency", "forgetting_job_lease_seconds", "query_rewrite_history_limit", "bot_aliases", "face_reaction_enabled", "face_reaction_id", "error_notify_enabled"]
CHAT_COLUMNS = ["proactive_enabled", "proactive_cooldown", "proactive_max_per_hour", "proactive_reservation_ttl_seconds", "reply_fulfillment_worker_interval_seconds", "reply_fulfillment_batch_size", "reply_fulfillment_lease_seconds", "reply_fulfillment_max_attempts", "reply_fulfillment_retry_base_seconds", "reply_fulfillment_tombstone_retention_days", "vision_image_download_max_count", "vision_image_download_max_bytes", "vision_image_download_total_max_bytes", "vision_image_download_max_pixels", "vision_image_download_concurrency", "vision_image_download_connect_timeout_seconds", "vision_image_download_read_timeout_seconds", "vision_image_download_total_timeout_seconds"]
RETIRED_TARGETS = (
    "komari_llm_provider_config",
    "komari_prompt_komari_chat",
    "komari_prompt_group_history_summary",
)
STAMP = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _load_converter() -> Any:
    spec = importlib.util.spec_from_file_location("tsk207_legacy_converter", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_historical_mappings_are_preserved_without_retired_destinations() -> None:
    specs = _load_converter()._RESOURCE_SPECS
    prompt = next(spec for spec in specs if spec.key_value == "komari_memory_summary")
    assert prompt.legacy_table == "komari_prompt_configs"
    assert prompt.legacy_key_column == "resource_id"
    assert prompt.legacy_data_column == "prompt_data"
    assert prompt.target_table == "komari_prompt_memory_summary"
    assert prompt.columns == PROMPT_FIELDS
    assert prompt.deprecated_keys == frozenset({"version", "last_updated", "display_name"})
    memory = next(spec for spec in specs if spec.target_table == "komari_memory_config")
    assert tuple(field for field in memory.columns if field.endswith("_summary")) == tuple(SUMMARY_VALUES)
    assert {spec.target_table for spec in specs if spec.key_value == "komari_memory"} == {
        "komari_memory_config", "komari_chat_config",
    }
    assert set(RETIRED_TARGETS).isdisjoint(spec.target_table for spec in specs)


@SKIP_NO_POSTGRES
async def test_cli_preserves_historical_values_and_unrelated_rows() -> None:
    scratch = await recreate_scratch_database("_tsk207_legacy_converter")
    try:
        conn = await asyncpg.connect(**scratch)
        try:
            await conn.execute(
                "CREATE TABLE komari_plugin_configs (plugin_name TEXT PRIMARY KEY, "
                "config_data JSONB NOT NULL, revision INTEGER, updated_at TIMESTAMPTZ);"
                "CREATE TABLE komari_prompt_configs (resource_id TEXT PRIMARY KEY, "
                "prompt_data JSONB NOT NULL, revision INTEGER, updated_at TIMESTAMPTZ)"
            )
            # Only supplied fields require their concrete historical SQL types.
            # Other fixture columns are nullable and must remain untouched.
            types = {
                "llm_temperature_summary": "DOUBLE PRECISION",
                "llm_max_tokens_summary": "INTEGER",
                "llm_stream_enabled_summary": "BOOLEAN",
                "llm_thinking_mode_summary": "BOOLEAN",
                "proactive_cooldown": "INTEGER",
                "reply_fulfillment_batch_size": "INTEGER",
            }
            targets = {
                "komari_memory_config": [*MEMORY_OTHER_COLUMNS, *SUMMARY_VALUES],
                "komari_chat_config": CHAT_COLUMNS,
                "komari_prompt_memory_summary": list(PROMPT_FIELDS),
            }
            for table, columns in targets.items():
                definition = ", ".join(f'"{name}" {types.get(name, "TEXT")}' for name in columns)
                await conn.execute(
                    f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, revision INTEGER, '
                    f'updated_at TIMESTAMPTZ, untouched TEXT, {definition})'
                )
                await conn.execute(
                    f'INSERT INTO "{table}" (id, revision, updated_at, untouched) '
                    "VALUES (2, 92, $1, 'unrelated-row-207')", STAMP,
                )
            for table in RETIRED_TARGETS:
                await conn.execute(f'CREATE TABLE "{table}" (sentinel TEXT)')
                await conn.execute(f'INSERT INTO "{table}" VALUES ($1)', "retired-target-207")
            before = {
                table: dict(await conn.fetchrow(f'SELECT * FROM "{table}" WHERE id = 2'))
                for table in targets
            }
            await conn.execute(
                "INSERT INTO komari_plugin_configs VALUES ('komari_memory', $1, 71, $2)",
                json.dumps({**SUMMARY_VALUES, "proactive_cooldown": 417, "reply_commit_batch_size": 23}),
                STAMP,
            )
            prompt_values = {name: f"historical-{name}-207" for name in PROMPT_FIELDS}
            await conn.execute(
                "INSERT INTO komari_prompt_configs VALUES ('komari_memory_summary', $1, 72, $2)",
                json.dumps(prompt_values), STAMP,
            )
            await conn.execute(
                "INSERT INTO komari_plugin_configs VALUES ('llm_provider', '{}', 81, $1)", STAMP,
            )
            for resource in ("komari_chat", "group_history_summary"):
                await conn.execute(
                    "INSERT INTO komari_prompt_configs VALUES ($1, $2, 82, $3)",
                    resource, json.dumps({"system_prompt": "must-not-import-207"}), STAMP,
                )
        finally:
            await conn.close()

        env = os.environ.copy()
        env["SQLALCHEMY_DATABASE_URL"] = scratch_url(str(scratch["database"]))
        env["LOG_LEVEL"] = "INFO"
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, str(SCRIPT)], cwd=ROOT, env=env,
            capture_output=True, text=True, check=False, timeout=60,
        )
        assert result.returncode == 0, "historical converter CLI failed"
        conn = await asyncpg.connect(**scratch)
        try:
            memory = dict(await conn.fetchrow("SELECT * FROM komari_memory_config WHERE id = 1"))
            assert {field: memory[field] for field in SUMMARY_VALUES} == SUMMARY_VALUES
            chat = dict(await conn.fetchrow("SELECT * FROM komari_chat_config WHERE id = 1"))
            assert chat["proactive_cooldown"] == 417
            assert chat["reply_fulfillment_batch_size"] == 23
            for row in (memory, chat):
                assert row["revision"] == 71
                assert row["updated_at"] == STAMP
            prompt = dict(await conn.fetchrow("SELECT * FROM komari_prompt_memory_summary WHERE id = 1"))
            assert {field: prompt[field] for field in PROMPT_FIELDS} == prompt_values
            assert prompt["revision"] == 72
            assert prompt["updated_at"] == STAMP
            for table in targets:
                assert dict(await conn.fetchrow(f'SELECT * FROM "{table}" WHERE id = 2')) == before[table]
                assert await conn.fetchval(f'SELECT count(*) FROM "{table}"') == 2
            for table in RETIRED_TARGETS:
                assert [dict(row) for row in await conn.fetch(f'SELECT * FROM "{table}"')] == [
                    {"sentinel": "retired-target-207"},
                ]
        finally:
            await conn.close()
    finally:
        await drop_scratch_database(str(scratch["database"]))
