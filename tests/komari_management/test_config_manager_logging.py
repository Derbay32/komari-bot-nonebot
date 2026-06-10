"""配置管理器日志安全测试。"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from komari_bot.plugins.config_manager import manager as manager_module
from komari_bot.plugins.config_manager.manager import ConfigManager
from komari_bot.plugins.config_manager.storage import StoredConfig

if TYPE_CHECKING:
    import pytest


class _ConfigSchema(BaseModel):
    api_token: str = "old-token"
    public_name: str = "old-name"


class _FakeStorage:
    def __init__(self) -> None:
        self.config_data: dict[str, Any] = {
            "api_token": "old-token",
            "public_name": "old-name",
        }

    def fetch(self, plugin_name: str) -> StoredConfig:
        return StoredConfig(
            plugin_name=plugin_name,
            schema_name=_ConfigSchema.__name__,
            config_data=self.config_data,
            version="1.0",
            updated_at=datetime.now().astimezone(),
        )

    def upsert(
        self,
        *,
        plugin_name: str,
        schema_name: str,
        config_data: dict[str, Any],
        version: str,
    ) -> StoredConfig:
        self.config_data = config_data
        return StoredConfig(
            plugin_name=plugin_name,
            schema_name=schema_name,
            config_data=config_data,
            version=version,
            updated_at=datetime.now().astimezone(),
        )


class _FakeLogger:
    def __init__(self) -> None:
        self.info_messages: list[str] = []

    def info(self, message: str) -> None:
        self.info_messages.append(message)

    def debug(self, _message: str) -> None:
        return None

    def warning(self, _message: str) -> None:
        return None


def test_update_field_log_does_not_include_new_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_storage = _FakeStorage()
    fake_logger = _FakeLogger()
    monkeypatch.setattr(manager_module, "get_config_storage", lambda: fake_storage)
    monkeypatch.setattr(manager_module, "logger", fake_logger)

    manager = ConfigManager("test_safe_logging", _ConfigSchema)
    manager.update_field("api_token", "new-sensitive-token")
    manager.update_field("public_name", "new-public-name")

    update_logs = [msg for msg in fake_logger.info_messages if "配置已更新" in msg]
    assert update_logs == [
        "[test_safe_logging] 配置已更新: api_token",
        "[test_safe_logging] 配置已更新: public_name",
    ]
    assert all("new-sensitive-token" not in msg for msg in update_logs)
    assert all("new-public-name" not in msg for msg in update_logs)
