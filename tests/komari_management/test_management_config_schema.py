"""Komari Management 配置 Schema 测试。"""

from __future__ import annotations

import importlib.util
from functools import cache
from pathlib import Path
from typing import Any, cast

import pytest

from komari_bot.plugins.komari_management.config_schema import DynamicConfigSchema


@cache
def _load_schema_class(plugin_name: str, class_name: str) -> type[Any]:
    module_path = (
        Path(__file__).resolve().parents[2]
        / "komari_bot"
        / "plugins"
        / plugin_name
        / "config_schema.py"
    )
    spec = importlib.util.spec_from_file_location(
        f"managed_{plugin_name}_config_schema",
        module_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast("type[Any]", getattr(module, class_name))


@pytest.mark.parametrize(
    ("plugin_name", "class_name", "expected_mode"),
    [
        ("komari_management", "DynamicConfigSchema", "restart"),
        ("embedding_provider", "DynamicConfigSchema", "restart"),
        ("komari_memory", "KomariMemoryConfigSchema", "immediate"),
        ("komari_sentry", "KomariSentryConfigSchema", "restart"),
        ("user_data", "DynamicConfigSchema", "restart"),
        ("group_history_summary", "DynamicConfigSchema", "immediate"),
        ("komari_decision", "KomariDecisionConfigSchema", "immediate"),
        ("komari_help", "DynamicConfigSchema", "immediate"),
        ("komari_knowledge", "DynamicConfigSchema", "immediate"),
        ("llm_provider", "DynamicConfigSchema", "immediate"),
        ("sr", "DynamicConfigSchema", "immediate"),
    ],
)
def test_managed_config_schemas_declare_default_apply_mode(
    plugin_name: str,
    class_name: str,
    expected_mode: str,
) -> None:
    schema = _load_schema_class(plugin_name, class_name)
    schema_extra = schema.model_config["json_schema_extra"]

    assert isinstance(schema_extra, dict)
    assert schema_extra["default_apply_mode"] == expected_mode


@pytest.mark.parametrize(
    ("plugin_name", "class_name", "field_name"),
    [
        ("komari_management", "DynamicConfigSchema", "api_token"),
        ("embedding_provider", "DynamicConfigSchema", "embedding_api_key"),
        ("embedding_provider", "DynamicConfigSchema", "rerank_api_key"),
        ("komari_sentry", "KomariSentryConfigSchema", "dsn"),
        ("llm_provider", "DynamicConfigSchema", "api_token"),
    ],
)
def test_managed_secret_fields_use_explicit_schema_metadata(
    plugin_name: str,
    class_name: str,
    field_name: str,
) -> None:
    schema = _load_schema_class(plugin_name, class_name)
    field_extra = schema.model_fields[field_name].json_schema_extra

    assert isinstance(field_extra, dict)
    assert field_extra["secret"] is True


def test_management_config_schema_parses_origin_list_string() -> None:
    config = DynamicConfigSchema(
        api_allowed_origins=cast(
            "Any",
            '["https://ui.example.com", "http://localhost:3000"]',
        ),
        announce_status_page_url="https://status.example.com/komari",
    )

    assert config.api_allowed_origins == [
        "https://ui.example.com",
        "http://localhost:3000",
    ]


def test_management_config_schema_defaults_are_safe() -> None:
    config = DynamicConfigSchema(
        announce_status_page_url="https://status.example.com/komari"
    )

    assert config.plugin_enable is False
    assert config.api_token == ""
    assert config.api_allowed_origins == []
    assert isinstance(config.announce_status_page_url, str)
    assert config.announce_status_page_url
    assert config.announce_max_group_count == 20
    assert config.announce_send_interval_seconds == 1.0
    assert config.announce_request_cooldown_seconds == 30.0


def test_management_config_schema_rejects_blank_status_page_url() -> None:
    with pytest.raises(ValueError, match="announce_status_page_url 不能为空"):
        DynamicConfigSchema(announce_status_page_url="   ")
