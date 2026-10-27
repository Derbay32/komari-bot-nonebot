"""Jev Prompt 强类型表与通用 Prompt 存储元数据契约。

聊天与总结提示词归 DSH；Jev 保留 PostgreSQL 单行资源。
字段白名单与严格校验仍由 Schema 定义，Python 不提供默认正文。
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
from komari_bot.plugins.komari_decision.prompt_schema import KomariDecisionPromptSchema

#: 每项 (resource_id, 表名, 模型)。
RESOURCE_MODELS: tuple[
    tuple[str, str, type[TypedConfigModel]],
    ...,
] = (
    ("komari_decision", "komari_prompt_komari_decision", KomariDecisionPromptSchema),

)

#: Prompt Schema 所在模块（TSK-191 断言 `DEFAULTS` 不再存在）。
PROMPT_SCHEMA_MODULE_NAMES: tuple[str, ...] = (
    "komari_bot.plugins.komari_decision.prompt_schema",
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

    # komari_decision 同时存在配置表与 Prompt 表，两个注册表互不干扰
    assert "komari_decision" in TYPED_CONFIG_MODEL_REGISTRY
    assert (
        TYPED_CONFIG_MODEL_REGISTRY["komari_decision"]
        is not TYPED_PROMPT_MODEL_REGISTRY["komari_decision"]
    )


def test_load_all_typed_models_covers_config_and_prompt_schemas() -> None:
    total = load_all_typed_config_models()

    assert total == len(TYPED_CONFIG_MODEL_REGISTRY) + len(TYPED_PROMPT_MODEL_REGISTRY)
    # TSK-279 新增 komari_roulette 配置资源；TSK-192 phase A 新增
    # komari_context 配置资源（迁移 0026）。
    # TSK-200/207 分别退役日志与模型网关配置资源（18 → 16）。
    # Management startup settings no longer register a database model.
    assert len(TYPED_CONFIG_MODEL_REGISTRY) == 15
    assert len(TYPED_PROMPT_MODEL_REGISTRY) == 1


def test_every_prompt_resource_is_a_distinct_typed_table() -> None:
    table_names = {
        model_cls.__table__.name
        for _resource_id, _table_name, model_cls in RESOURCE_MODELS
    }
    assert table_names == {
        "komari_prompt_komari_decision",
    }
    assert table_names <= set(SQLModel.metadata.tables)


@pytest.mark.parametrize("module_name", PROMPT_SCHEMA_MODULE_NAMES)
def test_prompt_schema_no_longer_defines_python_defaults(
    module_name: str,
) -> None:
    """AC2(TSK-191)：Prompt 资源的 Python 长文本 DEFAULTS 全部移除。"""
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
        KomariDecisionPromptSchema(role_description=123)  # pyright: ignore[reportArgumentType]


def test_each_prompt_resource_schema_defines_distinct_public_fields() -> None:
    """各资源字段集互不雷同，Schema 是各资源白名单的唯一来源。"""
    field_sets: list[set[str]] = [
        _public_fields(model_cls) for _rid, _tbl, model_cls in RESOURCE_MODELS
    ]
    assert len({frozenset(fields) for fields in field_sets}) == len(RESOURCE_MODELS)
