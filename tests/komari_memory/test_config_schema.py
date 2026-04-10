"""KomariMemory 配置清理测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from komari_bot.plugins.komari_memory.config_schema import KomariMemoryConfigSchema


def test_config_schema_no_longer_exposes_removed_bert_fields() -> None:
    fields = KomariMemoryConfigSchema.model_fields

    assert "bert_service_url" not in fields
    assert "bert_timeout" not in fields


def test_example_config_no_longer_contains_dead_fields() -> None:
    example_path = (
        Path(__file__).resolve().parents[2]
        / "config/config_manager/komari_memory_config.json.example"
    )
    config = json.loads(example_path.read_text(encoding="utf-8"))

    assert "bert_service_url" not in config
    assert "bert_timeout" not in config
    assert "llm_provider" not in config
    assert config["summary_chunk_token_limit"] == 3000
    assert config["profile_trait_limit"] == 20


def test_config_schema_exposes_summary_chunk_token_limit() -> None:
    config = KomariMemoryConfigSchema()

    assert config.api_enabled is True
    assert config.summary_chunk_token_limit == 3000
    assert config.profile_trait_limit == 20


def test_config_schema_rejects_too_small_summary_chunk_token_limit() -> None:
    with pytest.raises(ValueError):
        KomariMemoryConfigSchema(summary_chunk_token_limit=199)


def test_config_schema_rejects_too_small_profile_trait_limit() -> None:
    with pytest.raises(ValueError):
        KomariMemoryConfigSchema(profile_trait_limit=0)
