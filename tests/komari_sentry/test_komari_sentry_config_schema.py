"""KomariSentry 配置模型测试。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_LOADER_MODULE_NAME = "komari_bot.plugins.komari_sentry.config_schema"
"""与正常导入一致的规范模块名。

config_schema 已是 SQLModel ``table=True`` 强类型模型：SQLAlchemy 在类
定义阶段会把 ``ClassVar[str]`` 等字符串注解按 ``cls.__module__`` 解析
（``eval_name_only`` 查 ``sys.modules``）。裸文件加载若不注册
``sys.modules`` 会抛 ``MappedAnnotationError``；注册后再加载还会与
安全加载器（``typed_config``）形成两份类定义、触发注册表冲突。这里按
规范模块名注册且已加载时直接复用，两条路径都与运行时代码保持一致。
"""


def _load_config_schema_class() -> type:
    module = sys.modules.get(_LOADER_MODULE_NAME)
    if module is None:
        module_path = (
            Path(__file__).resolve().parents[2]
            / "komari_bot/plugins/komari_sentry/config_schema.py"
        )
        spec = importlib.util.spec_from_file_location(
            _LOADER_MODULE_NAME,
            module_path,
        )
        assert spec is not None and spec.loader is not None

        module = importlib.util.module_from_spec(spec)
        sys.modules[_LOADER_MODULE_NAME] = module
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
