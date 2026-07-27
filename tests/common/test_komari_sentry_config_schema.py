"""KomariSentry 配置模型测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_config_schema_class() -> type:
    module_path = (
        Path(__file__).resolve().parents[2]
        / "komari_bot/plugins/komari_sentry/config_schema.py"
    )
    spec = importlib.util.spec_from_file_location(
        "komari_sentry_config_schema",
        module_path,
    )
    assert spec is not None and spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.KomariSentryConfigSchema


KomariSentryConfigSchema = _load_config_schema_class()


def test_config_schema_exposes_sentry_logs_level() -> None:
    config = KomariSentryConfigSchema()

    assert config.sentry_logs_level == "WARNING"
    assert config.breadcrumb_level == "WARNING"


def test_config_schema_normalizes_sentry_logs_level() -> None:
    config = KomariSentryConfigSchema(sentry_logs_level="warning")

    assert config.sentry_logs_level == "WARNING"


def test_config_schema_falls_back_to_warning_for_invalid_log_levels() -> None:
    config = KomariSentryConfigSchema(sentry_logs_level="trace")

    assert config.sentry_logs_level == "WARNING"

    breadcrumb_config = KomariSentryConfigSchema(breadcrumb_level="trace")
    assert breadcrumb_config.breadcrumb_level == "WARNING"


def test_config_schema_default_contains_sentry_logs_level() -> None:
    config = KomariSentryConfigSchema()

    assert config.sentry_logs_level == "WARNING"


def test_send_default_pii_description_mentions_user_context_and_credential_sanitization() -> None:
    """send_default_pii 的 description 明确提及 user 上下文和凭据脱敏。"""
    properties = KomariSentryConfigSchema.model_json_schema()["properties"]
    description = properties["send_default_pii"]["description"]

    assert "user 上下文" in description
    assert "脱敏凭据" in description
