"""KomariMemory 配置清理测试。"""

from __future__ import annotations

import json
from pathlib import Path

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
