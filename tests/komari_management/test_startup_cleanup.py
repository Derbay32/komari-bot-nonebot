"""Komari Management v1 权限名启动提示测试。

旧版单 Token 键只存在于遗留 JSONB 表，强类型配置表不含该列；启动阶段
不再做旧 KV 清理，只保留旧权限名提示。
"""

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

    def _stored(self) -> StoredConfig | None:
        if self.config_data is None:
            return None
        return StoredConfig(
            plugin_name="komari_management",
            config_data=dict(self.config_data),
            revision=1,
            updated_at=datetime.now(UTC),
        )

    async def fetch_async(self, plugin_name: str) -> StoredConfig | None:
        assert plugin_name == "komari_management"
        return self._stored()


@pytest.mark.asyncio
async def test_startup_cleanup_no_longer_rewrites_stored_config() -> None:
    storage = _FakeStorage({"api_credentials": []})
    logger = _FakeLogger()

    await cleanup_management_v1_config(logger=logger, storage=storage)

    assert storage.config_data == {"api_credentials": []}
    assert logger.warning_messages == []


@pytest.mark.asyncio
async def test_startup_cleanup_without_config_is_silent() -> None:
    storage = _FakeStorage(None)
    logger = _FakeLogger()

    await cleanup_management_v1_config(logger=logger, storage=storage)

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
