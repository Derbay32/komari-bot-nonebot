"""TSK-197: KomariMemory keeps exactly the six live Hindsight fields."""

from __future__ import annotations

import pytest

from komari_bot.plugins.komari_memory.config_schema import KomariMemoryConfigSchema

LIVE_FIELDS = {
    "plugin_enable",
    "hindsight_base_url",
    "hindsight_api_key",
    "hindsight_bank_id",
    "hindsight_request_timeout_seconds",
    "hindsight_recovery_interval_seconds",
}

RETIRED_MEMORY_FIELDS = {
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


def test_memory_schema_exposes_exactly_the_live_hindsight_fields() -> None:
    assert set(KomariMemoryConfigSchema.model_fields) == {
        "id",
        "revision",
        "updated_at",
        *LIVE_FIELDS,
    }
    assert not RETIRED_MEMORY_FIELDS & set(KomariMemoryConfigSchema.model_fields)
    assert not RETIRED_MEMORY_FIELDS & set(KomariMemoryConfigSchema().model_dump())
    assert len(RETIRED_MEMORY_FIELDS) == 33


def test_memory_config_resource_is_optional_at_startup() -> None:
    assert KomariMemoryConfigSchema.startup_required is False
    assert "startup_required" not in KomariMemoryConfigSchema.model_fields
    assert "startup_required" not in KomariMemoryConfigSchema().model_dump()


def test_plugin_enable_defaults_false_and_applies_immediately() -> None:
    config = KomariMemoryConfigSchema()

    assert config.plugin_enable is False
    field_extra = KomariMemoryConfigSchema.model_fields[
        "plugin_enable"
    ].json_schema_extra
    assert isinstance(field_extra, dict)
    assert field_extra["apply_mode"] == "immediate"


def test_hindsight_config_defaults_and_secret_metadata() -> None:
    """TSK-193：Hindsight 连接字段默认空、超时/恢复间隔有显式默认值。"""
    config = KomariMemoryConfigSchema()

    assert config.hindsight_base_url == ""
    assert config.hindsight_api_key == ""
    assert config.hindsight_bank_id == ""
    assert config.hindsight_request_timeout_seconds == 10.0
    assert config.hindsight_recovery_interval_seconds == 30.0

    api_key_extra = KomariMemoryConfigSchema.model_fields[
        "hindsight_api_key"
    ].json_schema_extra
    assert isinstance(api_key_extra, dict)
    assert api_key_extra["secret"] is True

    for field_name in (
        "hindsight_base_url",
        "hindsight_api_key",
        "hindsight_bank_id",
        "hindsight_request_timeout_seconds",
        "hindsight_recovery_interval_seconds",
    ):
        field_extra = KomariMemoryConfigSchema.model_fields[
            field_name
        ].json_schema_extra
        assert isinstance(field_extra, dict)
        assert field_extra["apply_mode"] == "immediate"

    with pytest.raises(ValueError):
        KomariMemoryConfigSchema(hindsight_request_timeout_seconds=0.0)
    with pytest.raises(ValueError):
        KomariMemoryConfigSchema(hindsight_request_timeout_seconds=120.1)
    with pytest.raises(ValueError):
        KomariMemoryConfigSchema(hindsight_recovery_interval_seconds=0.0)
    with pytest.raises(ValueError):
        KomariMemoryConfigSchema(hindsight_recovery_interval_seconds=3600.1)


def test_hindsight_config_has_no_legacy_connection_aliases() -> None:
    """TSK-193：新字段不兼容旧连接字段，不提供别名或默认回退。"""
    fields = KomariMemoryConfigSchema.model_fields

    assert not {
        "hindsight_url",
        "hindsight_token",
        "hindsight_target_bank",
        "hindsight_timeout",
        "hindsight_retry_interval",
    } & set(fields)
