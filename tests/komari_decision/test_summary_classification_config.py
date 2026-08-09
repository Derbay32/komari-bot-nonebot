"""群总结场景归类配置契约测试（KOMARIBOT-22）。"""

from __future__ import annotations

from typing import Any, cast

from komari_bot.config.typed_config import (
    ensure_typed_config_model,
    get_config_section_metadata,
)


SUMMARY_CONFIG_DEFAULTS: dict[str, object] = {
    "summary_scene_top_k": 4,
    "summary_rerank_enabled": True,
    "summary_rerank_threshold": 0.6,
    "summary_similarity_threshold": None,
    "summary_rerank_fallback_enabled": False,
    "summary_rerank_failure_threshold": 3,
    "summary_rerank_failure_window_seconds": 3600,
}

SUMMARY_CONFIG_FIELDS = {
    "summary_embedding_instruction_query",
    "summary_rerank_instruction",
    *SUMMARY_CONFIG_DEFAULTS,
}


def _decision_schema() -> type[Any]:
    schema = ensure_typed_config_model("komari_decision")
    assert schema is not None
    return cast("type[Any]", schema)


def test_decision_config_declares_ordered_chat_and_summary_sections() -> None:
    """配置 Schema 以稳定 ID、展示名和顺序声明两个用途分区。"""
    metadata = get_config_section_metadata(_decision_schema())

    assert [
        (section.section_id, section.display_name, section.order)
        for section in metadata.sections
    ] == [
        ("chat_scene", "聊天场景", 10),
        ("summary_classification", "群总结归类", 20),
    ]

    for field_name in {
        "scene_top_k",
        "embedding_instruction_query",
        "embedding_instruction_scene",
        "rerank_instruction",
    }:
        assert metadata.field_section_ids[field_name] == "chat_scene"

    for field_name in SUMMARY_CONFIG_FIELDS:
        assert metadata.field_section_ids[field_name] == "summary_classification"


def test_summary_classification_config_has_safe_defaults() -> None:
    """总结归类默认使用 rerank，且不会隐式启用余弦 fallback。"""
    schema = _decision_schema()
    config = schema()

    for field_name, expected in SUMMARY_CONFIG_DEFAULTS.items():
        assert getattr(config, field_name) == expected

    assert getattr(config, "summary_embedding_instruction_query").strip()
    assert getattr(config, "summary_rerank_instruction").strip()


def test_summary_target_scene_identity_is_not_operator_configurable() -> None:
    """固定目标场景身份只能存在于判定实现，不能泄漏到管理配置。"""
    fields = set(_decision_schema().model_fields)

    assert "summary_scene_key" not in fields
    assert "summary_target_scene_key" not in fields
    assert "summary_scene_id" not in fields
