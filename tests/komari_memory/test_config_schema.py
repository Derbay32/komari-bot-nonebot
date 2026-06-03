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
