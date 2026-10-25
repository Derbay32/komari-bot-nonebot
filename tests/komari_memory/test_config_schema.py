"""KomariMemory 配置清理测试。"""

from __future__ import annotations

from komari_bot.plugins.komari_memory.config_schema import KomariMemoryConfigSchema


def test_config_schema_no_longer_exposes_removed_bert_fields() -> None:
    fields = KomariMemoryConfigSchema.model_fields

    assert "bert_service_url" not in fields
    assert "bert_timeout" not in fields


def test_config_schema_defaults_no_longer_contain_dead_fields() -> None:
    config = KomariMemoryConfigSchema().model_dump()

    assert "bert_service_url" not in config
    assert "bert_timeout" not in config
    assert "llm_provider" not in config
    assert "api_enabled" not in config
    assert config["profile_trait_limit"] == 20


def test_config_schema_exposes_profile_trait_limit() -> None:
    config = KomariMemoryConfigSchema()

    assert "api_enabled" not in KomariMemoryConfigSchema.model_fields
    assert config.profile_trait_limit == 20


def test_config_schema_declares_non_immediate_lifecycle_fields() -> None:
    expected_modes = {
        "plugin_enable": "restart",
        "global_interaction_summary_interval_minutes": "rebuild",
    }

    assert KomariMemoryConfigSchema.model_config.get("json_schema_extra") == {
        "default_apply_mode": "immediate"
    }
    for field_name, expected_mode in expected_modes.items():
        field_extra = KomariMemoryConfigSchema.model_fields[
            field_name
        ].json_schema_extra
        assert isinstance(field_extra, dict)
        assert field_extra["apply_mode"] == expected_mode


def test_config_schema_no_longer_owns_redis_db() -> None:
    """TSK-192：Redis 数据库编号由 komari_context 唯一拥有。"""

    assert "redis_db" not in KomariMemoryConfigSchema.model_fields
    assert "redis_db" not in KomariMemoryConfigSchema().model_dump()


def test_config_schema_rejects_too_small_profile_trait_limit() -> None:
    import pytest

    with pytest.raises(ValueError):
        KomariMemoryConfigSchema(profile_trait_limit=0)


MIGRATED_TO_KOMARI_CHAT_FIELDS = (
    "proactive_enabled",
    "proactive_score_threshold",
    "proactive_cooldown",
    "proactive_max_per_hour",
    "proactive_reservation_ttl_seconds",
    "reply_commit_worker_interval_seconds",
    "reply_commit_batch_size",
    "reply_commit_lease_seconds",
    "reply_commit_max_attempts",
    "reply_commit_retry_base_seconds",
    "reply_commit_tombstone_retention_days",
)

# TSK-194：图片理解模式与 8 项下载预算整体迁入 komari_chat_config；
# memory 侧物理删除（不保留 alias / 双读 / fallback）。
MIGRATED_TO_KOMARI_CHAT_VISION_FIELDS = (
    "vision_tool_enabled",
    "vision_image_download_max_count",
    "vision_image_download_max_bytes",
    "vision_image_download_total_max_bytes",
    "vision_image_download_max_pixels",
    "vision_image_download_concurrency",
    "vision_image_download_connect_timeout_seconds",
    "vision_image_download_read_timeout_seconds",
    "vision_image_download_total_timeout_seconds",
)


def test_config_schema_no_longer_exposes_migrated_proactive_fields() -> None:
    """频控/outbox 字段已迁入 komari_chat_config（KOMARIBOT-7）。"""
    fields = KomariMemoryConfigSchema.model_fields

    for field_name in MIGRATED_TO_KOMARI_CHAT_FIELDS:
        assert field_name not in fields


def test_config_schema_no_longer_exposes_migrated_vision_fields() -> None:
    """TSK-194：9 个旧图片字段从 komari_memory_config 物理删除。"""
    fields = KomariMemoryConfigSchema.model_fields

    for field_name in MIGRATED_TO_KOMARI_CHAT_VISION_FIELDS:
        assert field_name not in fields
    dumped = KomariMemoryConfigSchema().model_dump()
    for field_name in MIGRATED_TO_KOMARI_CHAT_VISION_FIELDS:
        assert field_name not in dumped


def test_interaction_processing_lease_has_bounded_immediate_config() -> None:
    import pytest

    config = KomariMemoryConfigSchema()
    field_extra = KomariMemoryConfigSchema.model_fields[
        "global_interaction_processing_lease_seconds"
    ].json_schema_extra

    assert config.global_interaction_processing_lease_seconds == 1800
    assert isinstance(field_extra, dict)
    assert field_extra["apply_mode"] == "immediate"
    with pytest.raises(ValueError):
        KomariMemoryConfigSchema(global_interaction_processing_lease_seconds=59)
    with pytest.raises(ValueError):
        KomariMemoryConfigSchema(global_interaction_processing_lease_seconds=7201)


def test_vision_image_download_budget_no_longer_lives_in_memory() -> None:
    """TSK-194：图片下载预算字段已迁入 komari_chat_config，memory 无对应列。"""
    fields = KomariMemoryConfigSchema.model_fields
    assert not {
        "vision_image_download_max_count",
        "vision_image_download_max_bytes",
        "vision_image_download_total_max_bytes",
        "vision_image_download_max_pixels",
        "vision_image_download_concurrency",
        "vision_image_download_connect_timeout_seconds",
        "vision_image_download_read_timeout_seconds",
        "vision_image_download_total_timeout_seconds",
    }.intersection(fields)


def test_config_schema_request_protocol_defaults() -> None:
    config = KomariMemoryConfigSchema()

    assert config.llm_request_api_summary == "chat_completions"
    assert config.llm_stream_enabled_summary is False


def test_config_schema_accepts_responses_request_api() -> None:
    config = KomariMemoryConfigSchema(
        llm_request_api_summary="responses",
        llm_stream_enabled_summary=True,
    )

    assert config.llm_request_api_summary == "responses"
    assert config.llm_stream_enabled_summary is True


def test_config_schema_rejects_invalid_request_api() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        KomariMemoryConfigSchema(llm_request_api_summary="websocket")  # type: ignore[arg-type]


TSK192_MOVED_TO_CHAT_FIELDS = {
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
}

TSK192_MOVED_TO_CONTEXT_FIELDS = {
    "bot_nickname",
    "error_notify_enabled",
    "global_interaction_enabled",
    "global_interaction_trigger_size",
}


def test_phase_b_moved_fields_have_no_memory_alias_or_dual_read() -> None:
    fields = set(KomariMemoryConfigSchema.model_fields)
    dumped = set(KomariMemoryConfigSchema().model_dump())

    assert not (TSK192_MOVED_TO_CHAT_FIELDS | TSK192_MOVED_TO_CONTEXT_FIELDS) & fields
    assert not (TSK192_MOVED_TO_CHAT_FIELDS | TSK192_MOVED_TO_CONTEXT_FIELDS) & dumped


def test_memory_keeps_only_its_active_old_algorithm_fields() -> None:
    fields = set(KomariMemoryConfigSchema.model_fields)

    assert {
        "plugin_enable",
        "llm_model_summary",
        "llm_temperature_summary",
        "llm_max_tokens_summary",
        "llm_request_api_summary",
        "llm_stream_enabled_summary",
        "llm_thinking_mode_summary",
        "llm_reasoning_effort_summary",
        "response_tag",
        "bot_aliases",
        "query_rewrite_history_limit",
        "global_interaction_summary_interval_minutes",
        "global_interaction_processing_lease_seconds",
    } <= fields


def test_memory_config_resource_is_optional_at_startup() -> None:
    assert KomariMemoryConfigSchema.startup_required is False
    assert "startup_required" not in KomariMemoryConfigSchema.model_fields
    assert "startup_required" not in KomariMemoryConfigSchema().model_dump()


def test_hindsight_config_defaults_and_secret_metadata() -> None:
    """TSK-193：Hindsight 连接字段默认空、超时/恢复间隔有显式默认值。"""
    import pytest

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
