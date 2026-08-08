"""Prompt 强类型表 Schema 契约测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import Text
from sqlmodel import SQLModel

from komari_bot.config.typed_config import (
    TYPED_CONFIG_MODEL_REGISTRY,
    TYPED_PROMPT_MODEL_REGISTRY,
    TypedConfigModel,
    TypedPromptModel,
    ensure_typed_prompt_model,
    load_all_typed_config_models,
)
from komari_bot.plugins.group_history_summary.prompt_schema import (
    DEFAULTS as GROUP_HISTORY_DEFAULTS,
)
from komari_bot.plugins.group_history_summary.prompt_schema import (
    GroupHistorySummaryPromptSchema,
)
from komari_bot.plugins.komari_chat.prompt_schema import (
    DEFAULTS as KOMARI_CHAT_DEFAULTS,
)
from komari_bot.plugins.komari_chat.prompt_schema import (
    KomariChatPromptSchema,
)
from komari_bot.plugins.komari_memory.prompt_schema import (
    DEFAULTS as MEMORY_SUMMARY_DEFAULTS,
)
from komari_bot.plugins.komari_memory.prompt_schema import (
    KomariMemorySummaryPromptSchema,
)

RESOURCE_MODELS: tuple[
    tuple[str, str, type[TypedConfigModel], dict[str, str]],
    ...,
] = (
    (
        "komari_chat",
        "komari_prompt_komari_chat",
        KomariChatPromptSchema,
        KOMARI_CHAT_DEFAULTS,
    ),
    (
        "komari_memory_summary",
        "komari_prompt_memory_summary",
        KomariMemorySummaryPromptSchema,
        MEMORY_SUMMARY_DEFAULTS,
    ),
    (
        "group_history_summary",
        "komari_prompt_group_history_summary",
        GroupHistorySummaryPromptSchema,
        GROUP_HISTORY_DEFAULTS,
    ),
)

INTERNAL_STORAGE_FIELDS = {"id", "revision", "updated_at"}


def test_prompt_models_register_in_prompt_registry_by_resource_id() -> None:
    """Prompt 表注册进独立注册表，不与配置资源共用 plugin_name 槽位。"""
    # 确定性加载全部 config_schema，避免依赖其他测试的导入副作用
    load_all_typed_config_models()

    for resource_id, _table_name, model_cls, _defaults in RESOURCE_MODELS:
        assert TYPED_PROMPT_MODEL_REGISTRY.get(resource_id) is model_cls
        assert issubclass(model_cls, TypedPromptModel)
        assert ensure_typed_prompt_model(resource_id) is model_cls

    # group_history_summary 同时存在配置表与 Prompt 表，两个注册表互不干扰
    assert "group_history_summary" in TYPED_CONFIG_MODEL_REGISTRY
    assert (
        TYPED_CONFIG_MODEL_REGISTRY["group_history_summary"]
        is not TYPED_PROMPT_MODEL_REGISTRY["group_history_summary"]
    )


def test_load_all_typed_models_covers_config_and_prompt_schemas() -> None:
    total = load_all_typed_config_models()

    assert total == len(TYPED_CONFIG_MODEL_REGISTRY) + len(
        TYPED_PROMPT_MODEL_REGISTRY
    )
    assert len(TYPED_CONFIG_MODEL_REGISTRY) == 15
    assert len(TYPED_PROMPT_MODEL_REGISTRY) == 3


def test_every_prompt_resource_is_a_distinct_typed_table() -> None:
    table_names = {
        model_cls.__table__.name
        for _resource_id, _table_name, model_cls, _defaults in RESOURCE_MODELS
    }
    assert table_names == {
        "komari_prompt_komari_chat",
        "komari_prompt_memory_summary",
        "komari_prompt_group_history_summary",
    }
    assert table_names <= set(SQLModel.metadata.tables)


def test_model_fields_match_runtime_defaults_exactly() -> None:
    """模型正文字段与运行时 DEFAULTS 键一一对应，防止表结构与模板漂移。"""
    for _resource_id, _table_name, model_cls, defaults in RESOURCE_MODELS:
        public_fields = set(model_cls.model_fields) - INTERNAL_STORAGE_FIELDS
        assert public_fields == set(defaults)


def test_prompt_columns_are_text() -> None:
    for _resource_id, table_name, model_cls, _defaults in RESOURCE_MODELS:
        table = model_cls.__table__
        assert table.name == table_name
        for column in table.columns:
            if column.name in INTERNAL_STORAGE_FIELDS:
                continue
            assert isinstance(column.type, Text)
            assert column.nullable is False


def test_storage_metadata_is_hidden_from_model_dump() -> None:
    for _resource_id, _table_name, model_cls, _defaults in RESOURCE_MODELS:
        dumped = model_cls().model_dump()
        assert INTERNAL_STORAGE_FIELDS.isdisjoint(dumped)
        assert "version" not in model_cls.model_fields


def test_prompt_model_keeps_strict_constructor_validation() -> None:
    with pytest.raises(ValidationError):
        KomariChatPromptSchema(system_prompt=123)  # pyright: ignore[reportArgumentType]
