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


def test_config_schema_rejects_too_small_profile_trait_limit() -> None:
    import pytest

    with pytest.raises(ValueError):
        KomariMemoryConfigSchema(profile_trait_limit=0)


def test_proactive_reservation_ttl_has_bounded_immediate_config() -> None:
    config = KomariMemoryConfigSchema()
    field_extra = KomariMemoryConfigSchema.model_fields[
        "proactive_reservation_ttl_seconds"
    ].json_schema_extra

    assert config.proactive_reservation_ttl_seconds == 360
    assert isinstance(field_extra, dict)
    assert field_extra["apply_mode"] == "immediate"

    import pytest

    with pytest.raises(ValueError):
        KomariMemoryConfigSchema(proactive_reservation_ttl_seconds=29)
    with pytest.raises(ValueError):
        KomariMemoryConfigSchema(proactive_reservation_ttl_seconds=901)


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
