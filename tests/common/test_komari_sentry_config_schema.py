"""KomariSentry 配置模型测试。"""

from __future__ import annotations

import importlib.util
import json
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

    assert config.sentry_logs_level == "INFO"


def test_config_schema_normalizes_sentry_logs_level() -> None:
    config = KomariSentryConfigSchema(sentry_logs_level="warning")

    assert config.sentry_logs_level == "WARNING"


def test_config_schema_falls_back_to_info_for_invalid_sentry_logs_level() -> None:
    config = KomariSentryConfigSchema(sentry_logs_level="trace")

    assert config.sentry_logs_level == "INFO"


def test_example_config_contains_sentry_logs_level() -> None:
    example_path = (
        Path(__file__).resolve().parents[2]
        / "config/config_manager/komari_sentry_config.json.example"
    )
    config = json.loads(example_path.read_text(encoding="utf-8"))

    assert config["sentry_logs_level"] == "INFO"
