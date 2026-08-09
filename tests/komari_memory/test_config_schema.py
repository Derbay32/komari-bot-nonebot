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
        "redis_db": "rebuild",
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


def test_config_schema_no_longer_exposes_migrated_proactive_fields() -> None:
    """频控/outbox 字段已迁入 komari_chat_config（KOMARIBOT-7）。"""
    fields = KomariMemoryConfigSchema.model_fields

    for field_name in MIGRATED_TO_KOMARI_CHAT_FIELDS:
        assert field_name not in fields


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


def test_vision_image_download_limits_are_bounded_and_immediate() -> None:
    config = KomariMemoryConfigSchema()
    expected_defaults = {
        "vision_image_download_max_count": 4,
        "vision_image_download_max_bytes": 8 * 1024 * 1024,
        "vision_image_download_total_max_bytes": 20 * 1024 * 1024,
        "vision_image_download_max_pixels": 40_000_000,
        "vision_image_download_concurrency": 2,
        "vision_image_download_connect_timeout_seconds": 5.0,
        "vision_image_download_read_timeout_seconds": 30.0,
        "vision_image_download_total_timeout_seconds": 45.0,
    }

    for field_name, expected_default in expected_defaults.items():
        assert getattr(config, field_name) == expected_default
        field_extra = KomariMemoryConfigSchema.model_fields[
            field_name
        ].json_schema_extra
        assert isinstance(field_extra, dict)
        assert field_extra["apply_mode"] == "immediate"


def test_vision_image_download_rejects_inconsistent_batch_budgets() -> None:
    import pytest

    with pytest.raises(ValueError, match="总字节上限"):
        KomariMemoryConfigSchema(
            vision_image_download_max_bytes=2 * 1024 * 1024,
            vision_image_download_total_max_bytes=1024 * 1024,
        )
    with pytest.raises(ValueError, match="总时限"):
        KomariMemoryConfigSchema(
            vision_image_download_connect_timeout_seconds=10,
            vision_image_download_total_timeout_seconds=5,
        )


def test_config_schema_request_protocol_defaults() -> None:
    config = KomariMemoryConfigSchema()

    assert config.llm_request_api_chat == "chat_completions"
    assert config.llm_stream_enabled_chat is False
    assert config.llm_request_api_summary == "chat_completions"
    assert config.llm_stream_enabled_summary is False


def test_config_schema_accepts_responses_request_api() -> None:
    config = KomariMemoryConfigSchema(
        llm_request_api_chat="responses",
        llm_stream_enabled_chat=True,
        llm_request_api_summary="responses",
        llm_stream_enabled_summary=True,
    )

    assert config.llm_request_api_chat == "responses"
    assert config.llm_stream_enabled_chat is True
    assert config.llm_request_api_summary == "responses"
    assert config.llm_stream_enabled_summary is True


def test_config_schema_rejects_invalid_request_api() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        KomariMemoryConfigSchema(llm_request_api_chat="websocket")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        KomariMemoryConfigSchema(llm_request_api_summary="websocket")  # type: ignore[arg-type]
