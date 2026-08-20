"""Prompt 强类型表 Schema 契约测试。

TSK-191：三个 Prompt 资源（chat / memory summary / group history summary）
全部以 PostgreSQL 为运行时真源，字段集合由强类型 Schema 决定；三者都不再
定义 Python 长文本 ``DEFAULTS``（AC2），旧的默认机制与临时兼容分支被物理
删除（AC8：``ManagedPromptResource`` 不再有 ``defaults`` 字段、
``PromptTemplateLoader`` 构造不再接受 ``defaults`` 参数）。因此本文件不再
import 任何 ``DEFAULTS`` 符号，字段集一律直接派生自 Schema。
"""

from __future__ import annotations

import importlib

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
    GroupHistorySummaryPromptSchema,
)
from komari_bot.plugins.komari_chat.prompt_schema import (
    KomariChatPromptSchema,
)
from komari_bot.plugins.komari_memory.prompt_schema import (
    KomariMemorySummaryPromptSchema,
)

#: 每项 (resource_id, 表名, 模型)。
RESOURCE_MODELS: tuple[
    tuple[str, str, type[TypedConfigModel]],
    ...,
] = (
    (
        "komari_chat",
        "komari_prompt_komari_chat",
        KomariChatPromptSchema,
    ),
    (
        "komari_memory_summary",
        "komari_prompt_memory_summary",
        KomariMemorySummaryPromptSchema,
    ),
    (
        "group_history_summary",
        "komari_prompt_group_history_summary",
        GroupHistorySummaryPromptSchema,
    ),
)

#: Prompt Schema 所在模块（TSK-191 断言 `DEFAULTS` 不再存在）。
PROMPT_SCHEMA_MODULE_NAMES: tuple[str, ...] = (
    "komari_bot.plugins.komari_chat.prompt_schema",
    "komari_bot.plugins.komari_memory.prompt_schema",
    "komari_bot.plugins.group_history_summary.prompt_schema",
)

INTERNAL_STORAGE_FIELDS = {"id", "revision", "updated_at"}


def _public_fields(model_cls: type[TypedConfigModel]) -> set[str]:
    """Prompt 表正文字段名（不含存储专用字段）。"""
    return set(model_cls.model_fields) - INTERNAL_STORAGE_FIELDS


def test_prompt_models_register_in_prompt_registry_by_resource_id() -> None:
    """Prompt 表注册进独立注册表，不与配置资源共用 plugin_name 槽位。"""
    # 确定性加载全部 config_schema，避免依赖其他测试的导入副作用
    load_all_typed_config_models()

    for resource_id, _table_name, model_cls in RESOURCE_MODELS:
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
        for _resource_id, _table_name, model_cls in RESOURCE_MODELS
    }
    assert table_names == {
        "komari_prompt_komari_chat",
        "komari_prompt_memory_summary",
        "komari_prompt_group_history_summary",
    }
    assert table_names <= set(SQLModel.metadata.tables)


@pytest.mark.parametrize("module_name", PROMPT_SCHEMA_MODULE_NAMES)
def test_prompt_schema_no_longer_defines_python_defaults(
    module_name: str,
) -> None:
    """AC2(TSK-191)：三个 Prompt 资源的 Python 长文本 DEFAULTS 全部移除。

    当前 memory/group 的 prompt_schema 仍定义并导出 ``DEFAULTS``，因此
    这些参数是 TSK-191 的可解释 RED。
    """
    module = importlib.import_module(module_name)

    assert "DEFAULTS" not in vars(module), (
        f"{module_name} 不得继续定义/导出 DEFAULTS"
        "（Python 长文本默认正文已全部迁入版本化初始数据）"
    )


def test_managed_prompt_resource_no_longer_has_defaults_field() -> None:
    """AC8(TSK-191)：ManagedPromptResource 不再携带 defaults 字段。

    管理资源只承载 resource_id/display_name，字段集合由强类型 Schema
    决定。当前实现仍有 ``defaults`` 字段，本用例是 TSK-191 的可解释 RED。
    """
    from komari_bot.plugins.komari_management.managed_resources import (
        ManagedPromptResource,
    )

    assert "defaults" not in ManagedPromptResource.__dataclass_fields__


def test_prompt_columns_are_text() -> None:
    for _resource_id, table_name, model_cls in RESOURCE_MODELS:
        table = model_cls.__table__
        assert table.name == table_name
        for column in table.columns:
            if column.name in INTERNAL_STORAGE_FIELDS:
                continue
            assert isinstance(column.type, Text)
            assert column.nullable is False


def test_storage_metadata_is_hidden_from_model_dump() -> None:
    for _resource_id, _table_name, model_cls in RESOURCE_MODELS:
        dumped = model_cls().model_dump()
        assert INTERNAL_STORAGE_FIELDS.isdisjoint(dumped)
        assert "version" not in model_cls.model_fields


def test_prompt_model_keeps_strict_constructor_validation() -> None:
    with pytest.raises(ValidationError):
        KomariChatPromptSchema(system_prompt=123)  # pyright: ignore[reportArgumentType]


def test_each_prompt_resource_schema_defines_distinct_public_fields() -> None:
    """三个资源字段集互不雷同，Schema 是各资源白名单的唯一来源。"""
    field_sets: list[set[str]] = [
        _public_fields(model_cls) for _rid, _tbl, model_cls in RESOURCE_MODELS
    ]
    assert len({frozenset(fields) for fields in field_sets}) == 3


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

    resolved: dict[str, str] = resolve_behavior_field_names(public_fields)
    assert set(resolved.values()).isdisjoint(REQUIRED_EXACT_FIELDS), (
        "工具调用/图片读取职责之外的独立字段不得与精确点名字段复用"
    )
