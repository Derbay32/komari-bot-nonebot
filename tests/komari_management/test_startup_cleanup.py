"""Komari Management v1 配置残留启动清理测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from komari_bot.plugins.config_manager.storage import StoredConfig
from komari_bot.plugins.komari_management.startup_cleanup import (
    cleanup_management_v1_config,
)


class _FakeLogger:
    def __init__(self) -> None:
        self.warning_messages: list[str] = []

    def warning(self, message: str) -> None:
        self.warning_messages.append(message)


class _FakeStorage:
    def __init__(self, config_data: dict[str, Any] | None) -> None:
        self.config_data = config_data
        self.delete_calls: list[tuple[str, str]] = []

    def _stored(self) -> StoredConfig | None:
        if self.config_data is None:
            return None
        return StoredConfig(
            plugin_name="komari_management",
            schema_name="DynamicConfigSchema",
            config_data=dict(self.config_data),
            version="2.0",
            revision=1,
            updated_at=datetime.now(UTC),
        )

    async def fetch_async(self, plugin_name: str) -> StoredConfig | None:
        assert plugin_name == "komari_management"
        return self._stored()

    async def delete_field_if_present_async(
        self,
        *,
        plugin_name: str,
        field_name: str,
    ) -> StoredConfig | None:
        self.delete_calls.append((plugin_name, field_name))
        if self.config_data is None or field_name not in self.config_data:
            return None
        del self.config_data[field_name]
        return self._stored()


@pytest.mark.asyncio
async def test_startup_cleanup_removes_legacy_token_and_warns() -> None:
    storage = _FakeStorage(
        {
            "api_token": "legacy-token-000000",
            "api_credentials": [],
        }
    )
    logger = _FakeLogger()

    await cleanup_management_v1_config(logger=logger, storage=storage)

    assert storage.config_data == {"api_credentials": []}
    assert logger.warning_messages == [
        "[Komari Management] 已删除废弃的 api_token 配置键，"
        "请使用 api_credentials 配置管理凭据"
    ]


@pytest.mark.asyncio
async def test_startup_cleanup_without_legacy_values_is_silent() -> None:
    storage = _FakeStorage({"api_credentials": []})
    logger = _FakeLogger()

    await cleanup_management_v1_config(logger=logger, storage=storage)

    assert storage.config_data == {"api_credentials": []}
    assert logger.warning_messages == []


@pytest.mark.asyncio
async def test_startup_cleanup_warns_about_old_permission_without_rewriting() -> None:
    credentials = [
        {
            "credential_id": "dashboard",
            "token": "dashboard-token-000000",
            "permissions": ["config:read", "llm_logs:read"],
        }
    ]
    storage = _FakeStorage({"api_credentials": credentials})
    logger = _FakeLogger()

    await cleanup_management_v1_config(logger=logger, storage=storage)

    assert storage.config_data == {"api_credentials": credentials}
    assert logger.warning_messages == [
        "[Komari Management] 检测到旧权限名 llm_logs:read，"
        "请手动改为 agent_run_logs:read"
    ]
