"""Komari Management 配置 Schema 测试。"""

from __future__ import annotations

from typing import Any, cast

from komari_bot.plugins.komari_management.config_schema import DynamicConfigSchema


def test_management_config_schema_parses_origin_list_string() -> None:
    config = DynamicConfigSchema(
        api_allowed_origins=cast(
            "Any",
            '["https://ui.example.com", "http://localhost:3000"]',
        )
    )

    assert config.api_allowed_origins == [
        "https://ui.example.com",
        "http://localhost:3000",
    ]


def test_management_config_schema_defaults_are_safe() -> None:
    config = DynamicConfigSchema()

    assert config.plugin_enable is False
    assert config.api_token == ""
    assert config.api_allowed_origins == []
