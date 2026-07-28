"""LLM provider 与持久 Agent Run 日志职责解耦测试。"""

from __future__ import annotations

import inspect
from pathlib import Path

from komari_bot.plugins.llm_provider.config_schema import DynamicConfigSchema


def test_provider_schema_does_not_expose_legacy_log_fields() -> None:
    fields = DynamicConfigSchema.model_fields
    schema = DynamicConfigSchema.model_json_schema()
    for name in ("llm_log_retention_days", "llm_log_dir_permission_mode"):
        assert name not in fields
        assert name not in schema["properties"]
        assert name not in DynamicConfigSchema().model_dump()


def test_provider_entrypoint_has_no_persistent_logger_dependency() -> None:
    plugin_dir = Path(inspect.getfile(DynamicConfigSchema)).parent
    source = (plugin_dir / "__init__.py").read_text(encoding="utf-8")
    assert "record_chat_log" not in source
    assert "agent_run_logger" not in source
    assert "reply_log" not in source
    assert "llm_logger" not in source
    for deleted in (
        "api.py",
        "diagnostic.py",
        "llm_logger.py",
        "reply_log_index.py",
        "reply_log_reader.py",
    ):
        assert not (plugin_dir / deleted).exists()
