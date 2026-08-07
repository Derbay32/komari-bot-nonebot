"""动态配置 SQLModel 强类型表契约。"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import SQLModel

if TYPE_CHECKING:
    from komari_bot.config.typed_config import TypedConfigModel

CONFIG_SCHEMA_MODULES = (
    "komari_bot.plugins.agent_run_logger.config_schema",
    "komari_bot.plugins.embedding_provider.config_schema",
    "komari_bot.plugins.group_history_summary.config_schema",
    "komari_bot.plugins.komari_custom.config_schema",
    "komari_bot.plugins.komari_decision.config_schema",
    "komari_bot.plugins.komari_help.config_schema",
    "komari_bot.plugins.komari_knowledge.config_schema",
    "komari_bot.plugins.komari_management.config_schema",
    "komari_bot.plugins.komari_memory.config_schema",
    "komari_bot.plugins.komari_search.config_schema",
    "komari_bot.plugins.komari_sentry.config_schema",
    "komari_bot.plugins.llm_provider.config_schema",
    "komari_bot.plugins.sr.config_schema",
    "komari_bot.plugins.user_data.config_schema",
)
INTERNAL_STORAGE_FIELDS = {"id", "revision", "updated_at"}


def _config_models() -> list[type[TypedConfigModel]]:
    models: list[type[TypedConfigModel]] = []
    for module_name in CONFIG_SCHEMA_MODULES:
        module = import_module(module_name)
        schema = getattr(
            module,
            "KomariMemoryConfigSchema",
            getattr(
                module,
                "KomariDecisionConfigSchema",
                getattr(
                    module,
                    "KomariSentryConfigSchema",
                    getattr(module, "AgentRunLoggerConfigSchema", None),
                ),
            ),
        )
        if schema is None:
            schema = module.DynamicConfigSchema
        models.append(schema)
    return models


def test_every_dynamic_config_is_a_distinct_typed_table() -> None:
    models = _config_models()

    assert len(models) == 14
    assert all(issubclass(model, SQLModel) for model in models)
    assert all(hasattr(model, "__table__") for model in models)
    table_names = {model.__table__.name for model in models}
    assert len(table_names) == len(models)
    assert table_names <= set(SQLModel.metadata.tables)


def test_storage_metadata_is_hidden_and_legacy_metadata_is_removed() -> None:
    for model in _config_models():
        assert "version" not in model.model_fields
        assert "last_updated" not in model.model_fields
        assert INTERNAL_STORAGE_FIELDS <= set(model.model_fields)
        dumped = model().model_dump()
        assert INTERNAL_STORAGE_FIELDS.isdisjoint(dumped)


def test_table_constructor_keeps_pydantic_validation() -> None:
    schema = import_module(
        "komari_bot.plugins.llm_provider.config_schema"
    ).DynamicConfigSchema

    with pytest.raises(ValidationError):
        schema(chat_rpm_limit=0)


def test_extensible_config_fields_use_jsonb_columns() -> None:
    llm_schema = import_module(
        "komari_bot.plugins.llm_provider.config_schema"
    ).DynamicConfigSchema
    summary_schema = import_module(
        "komari_bot.plugins.group_history_summary.config_schema"
    ).DynamicConfigSchema

    assert isinstance(llm_schema.__table__.c.extra_params.type, JSONB)
    assert isinstance(summary_schema.__table__.c.layout_params.type, JSONB)
