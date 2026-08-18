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
    KomariChatPromptSchema,
)
from komari_bot.plugins.komari_memory.prompt_schema import (
    DEFAULTS as MEMORY_SUMMARY_DEFAULTS,
)
from komari_bot.plugins.komari_memory.prompt_schema import (
    KomariMemorySummaryPromptSchema,
)

#: 每项 (resource_id, 表名, 模型, DEFAULTS)。komari_chat 的 DEFAULTS 随
#: AC1 移除（Python 长文本默认正文不再提供聊天 Prompt），故为 None。
RESOURCE_MODELS: tuple[
    tuple[str, str, type[TypedConfigModel], dict[str, str] | None],
    ...,
] = (
    (
        "komari_chat",
        "komari_prompt_komari_chat",
        KomariChatPromptSchema,
        None,
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
    """模型正文字段与运行时 DEFAULTS 键一一对应，防止表结构与模板漂移。

    komari_chat 的 DEFAULTS 已随 AC1 移除（其字段集由 seed 契约测试与
    Schema 责任解析验证）；memory/group 的 DEFAULTS 保留到 TSK-191。
    """
    for _resource_id, _table_name, model_cls, defaults in RESOURCE_MODELS:
        public_fields = set(model_cls.model_fields) - INTERNAL_STORAGE_FIELDS
        if defaults is None:
            continue
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


def test_komari_chat_schema_no_longer_defines_python_defaults() -> None:
    """AC1：聊天 Prompt 初始值不再由 Python 长文本默认字典 DEFAULTS 提供。

    只要求 chat 资源先移除 DEFAULTS；memory/group 的 DEFAULTS 仍保留
    （TSK-191 再统一移除 generic defaults 参数，本 ticket 不迫使
    PromptTemplateLoader 全局 API 一次性破坏）。当前实现仍定义
    DEFAULTS，因此本用例是 TSK-190 的可解释 RED。
    """
    import komari_bot.plugins.komari_chat.prompt_schema as chat_schema

    assert "DEFAULTS" not in vars(chat_schema), (
        "聊天 Prompt 不得继续定义/导出 DEFAULTS（Python 长文本默认正文移除）"
    )
    assert MEMORY_SUMMARY_DEFAULTS, "TSK-191 之前 memory DEFAULTS 仍应存在"
    assert GROUP_HISTORY_DEFAULTS, "TSK-191 之前 group DEFAULTS 仍应存在"


def test_prompt_model_keeps_strict_constructor_validation() -> None:
    with pytest.raises(ValidationError):
        KomariChatPromptSchema(system_prompt=123)  # pyright: ignore[reportArgumentType]


def test_komari_chat_schema_replaces_output_instruction_with_behavior_fields() -> None:
    """AC2：聊天 Prompt Schema 删除 output_instruction，新增独立行为字段。

    精确点名字段（tool_call_instruction / image_read_instruction）按名称
    断言；其余职责只断言存在互不相同的独立字段（见 oracle 模块）。
    """
    from tests.config.chat_prompt_field_contract import (
        REMOVED_FIELD,
        REQUIRED_EXACT_FIELDS,
        chat_prompt_field_names,
        resolve_behavior_field_names,
    )

    public_fields = chat_prompt_field_names()

    assert REMOVED_FIELD not in public_fields
    assert set(REQUIRED_EXACT_FIELDS) <= public_fields

    resolved = resolve_behavior_field_names(public_fields)
    assert set(resolved.values()).isdisjoint(REQUIRED_EXACT_FIELDS), (
        "工具调用/图片读取职责之外的独立字段不得与精确点名字段复用"
    )
