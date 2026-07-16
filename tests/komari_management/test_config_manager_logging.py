"""配置管理器日志安全测试。"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

import pytest
from pydantic import BaseModel

from komari_bot.plugins.config_manager import manager as manager_module
from komari_bot.plugins.config_manager.manager import ConfigManager
from komari_bot.plugins.config_manager.storage import StoredConfig

if TYPE_CHECKING:
    from collections.abc import Iterator


class _ConfigSchema(BaseModel):
    api_token: str = "old-token"
    public_name: str = "old-name"


class _NormalizedConfigSchema(BaseModel):
    retry_count: int = 3
    public_name: str = "默认名称"


class _FakeStorage:
    def __init__(
        self,
        config_data: dict[str, Any] | None = None,
        *,
        fail_upsert: bool = False,
        conflict_data: dict[str, Any] | None = None,
    ) -> None:
        self.config_data: dict[str, Any] = config_data or {
            "api_token": "old-token",
            "public_name": "old-name",
        }
        self.fail_upsert = fail_upsert
        self.conflict_data = conflict_data
        self.revision = 1
        self.saved_payloads: list[dict[str, Any]] = []

    def fetch(self, plugin_name: str) -> StoredConfig:
        return StoredConfig(
            plugin_name=plugin_name,
            schema_name="TestSchema",
            config_data=self.config_data,
            version="1.0",
            revision=self.revision,
            updated_at=datetime.now().astimezone(),
        )

    async def fetch_async(self, plugin_name: str) -> StoredConfig:
        return self.fetch(plugin_name)

    def upsert(
        self,
        *,
        plugin_name: str,
        schema_name: str,
        config_data: dict[str, Any],
        version: str,
    ) -> StoredConfig:
        if self.fail_upsert:
            msg = "写入失败"
            raise RuntimeError(msg)
        self.config_data = config_data
        self.revision += 1
        self.saved_payloads.append(config_data)
        return StoredConfig(
            plugin_name=plugin_name,
            schema_name=schema_name,
            config_data=config_data,
            version=version,
            revision=self.revision,
            updated_at=datetime.now().astimezone(),
        )

    async def upsert_async(
        self,
        *,
        plugin_name: str,
        schema_name: str,
        config_data: dict[str, Any],
        version: str,
    ) -> StoredConfig:
        return self.upsert(
            plugin_name=plugin_name,
            schema_name=schema_name,
            config_data=config_data,
            version=version,
        )

    def update_if_unchanged(
        self,
        *,
        plugin_name: str,
        schema_name: str,
        config_data: dict[str, Any],
        version: str,
        expected_updated_at: datetime,
    ) -> StoredConfig | None:
        del expected_updated_at
        if self.conflict_data is not None:
            self.config_data = self.conflict_data
            return None
        return self.upsert(
            plugin_name=plugin_name,
            schema_name=schema_name,
            config_data=config_data,
            version=version,
        )

    async def update_if_unchanged_async(
        self,
        *,
        plugin_name: str,
        schema_name: str,
        config_data: dict[str, Any],
        version: str,
        expected_updated_at: datetime,
    ) -> StoredConfig | None:
        return self.update_if_unchanged(
            plugin_name=plugin_name,
            schema_name=schema_name,
            config_data=config_data,
            version=version,
            expected_updated_at=expected_updated_at,
        )

    def update_fields_if_revision(
        self,
        *,
        plugin_name: str,
        schema_name: str,
        config_patch: dict[str, Any],
        version: str,
        expected_revision: int,
    ) -> StoredConfig | None:
        if expected_revision != self.revision:
            return None
        updated_data = {**self.config_data, **config_patch}
        return self.upsert(
            plugin_name=plugin_name,
            schema_name=schema_name,
            config_data=updated_data,
            version=version,
        )

    async def update_fields_if_revision_async(
        self,
        *,
        plugin_name: str,
        schema_name: str,
        config_patch: dict[str, Any],
        version: str,
        expected_revision: int,
    ) -> StoredConfig | None:
        return self.update_fields_if_revision(
            plugin_name=plugin_name,
            schema_name=schema_name,
            config_patch=config_patch,
            version=version,
            expected_revision=expected_revision,
        )


class _FakeLogger:
    def __init__(self) -> None:
        self.info_messages: list[str] = []
        self.warning_messages: list[str] = []

    def info(self, message: str) -> None:
        self.info_messages.append(message)

    def debug(self, _message: str) -> None:
        return None

    def warning(self, _message: str) -> None:
        self.warning_messages.append(_message)

    def error(self, _message: str) -> None:
        self.warning_messages.append(_message)


class _SingleConflictStorage(_FakeStorage):
    def __init__(self) -> None:
        super().__init__()
        self.conflict_injected = False

    def update_fields_if_revision(
        self,
        *,
        plugin_name: str,
        schema_name: str,
        config_patch: dict[str, Any],
        version: str,
        expected_revision: int,
    ) -> StoredConfig | None:
        if not self.conflict_injected:
            self.conflict_injected = True
            self.config_data = {**self.config_data, "public_name": "并发更新值"}
            self.revision += 1
            return None
        return super().update_fields_if_revision(
            plugin_name=plugin_name,
            schema_name=schema_name,
            config_patch=config_patch,
            version=version,
            expected_revision=expected_revision,
        )


@pytest.fixture(autouse=True)
def _clear_config_manager_singletons() -> Iterator[None]:
    manager_module._config_managers.clear()
    yield
    manager_module._config_managers.clear()


def test_initialize_syncs_added_removed_and_converted_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_storage = _FakeStorage(
        {
            "retry_count": "7",
            "legacy_field": "旧字段",
        }
    )
    fake_logger = _FakeLogger()
    monkeypatch.setattr(manager_module, "get_config_storage", lambda: fake_storage)
    monkeypatch.setattr(manager_module, "logger", fake_logger)

    manager = ConfigManager("test_normalize_initialize", _NormalizedConfigSchema)
    config = cast("_NormalizedConfigSchema", manager.initialize())

    assert isinstance(config, _NormalizedConfigSchema)
    assert config.retry_count == 7
    assert config.public_name == "默认名称"
    assert fake_storage.saved_payloads == [
        {"retry_count": 7, "legacy_field": "旧字段", "public_name": "默认名称"}
    ]
    assert fake_storage.config_data == {
        "retry_count": 7,
        "legacy_field": "旧字段",
        "public_name": "默认名称",
    }
    assert any("sync_result=success" in msg for msg in fake_logger.info_messages)


def test_reload_syncs_stored_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_storage = _FakeStorage({"legacy_field": "旧字段"})
    monkeypatch.setattr(manager_module, "get_config_storage", lambda: fake_storage)

    manager = ConfigManager("test_normalize_reload", _NormalizedConfigSchema)
    config = cast("_NormalizedConfigSchema", manager.reload())

    assert config.retry_count == 3
    assert fake_storage.saved_payloads == [
        {"legacy_field": "旧字段", "retry_count": 3, "public_name": "默认名称"}
    ]


def test_sync_failure_keeps_memory_config_and_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_storage = _FakeStorage({"legacy_field": "旧字段"}, fail_upsert=True)
    fake_logger = _FakeLogger()
    monkeypatch.setattr(manager_module, "get_config_storage", lambda: fake_storage)
    monkeypatch.setattr(manager_module, "logger", fake_logger)

    manager = ConfigManager("test_normalize_failure", _NormalizedConfigSchema)
    config = cast("_NormalizedConfigSchema", manager.initialize())

    assert config.retry_count == 3
    assert fake_storage.saved_payloads == []
    assert fake_storage.config_data == {"legacy_field": "旧字段"}
    assert any("sync_result=failed" in msg for msg in fake_logger.warning_messages)


def test_sync_conflict_uses_latest_config_without_overwriting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_storage = _FakeStorage(
        {"legacy_field": "旧字段"},
        conflict_data={"retry_count": 9, "public_name": "管理员新值"},
    )
    fake_logger = _FakeLogger()
    monkeypatch.setattr(manager_module, "get_config_storage", lambda: fake_storage)
    monkeypatch.setattr(manager_module, "logger", fake_logger)

    manager = ConfigManager("test_normalize_conflict", _NormalizedConfigSchema)
    config = cast("_NormalizedConfigSchema", manager.initialize())

    assert config.retry_count == 9
    assert config.public_name == "管理员新值"
    assert fake_storage.saved_payloads == []
    assert any("reason=stored_changed" in msg for msg in fake_logger.warning_messages)


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


@pytest.mark.asyncio
async def test_async_field_updates_from_two_workers_preserve_each_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_storage = _FakeStorage()
    monkeypatch.setattr(manager_module, "get_config_storage", lambda: fake_storage)

    first_manager = ConfigManager("test_multi_worker", _ConfigSchema)
    await first_manager.initialize_async()

    second_manager = ConfigManager("test_multi_worker", _ConfigSchema)
    await second_manager.initialize_async()

    await first_manager.update_field_async("api_token", "new-token")
    await second_manager.update_field_async("public_name", "new-name")

    assert fake_storage.config_data == {
        "api_token": "new-token",
        "public_name": "new-name",
    }


@pytest.mark.asyncio
async def test_async_field_update_reloads_and_retries_after_revision_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_storage = _SingleConflictStorage()
    monkeypatch.setattr(manager_module, "get_config_storage", lambda: fake_storage)

    manager = ConfigManager("test_revision_retry", _ConfigSchema)
    updated = cast(
        "_ConfigSchema",
        await manager.update_field_async("api_token", "new-token"),
    )

    assert fake_storage.conflict_injected is True
    assert updated.api_token == "new-token"
    assert updated.public_name == "并发更新值"
    assert fake_storage.config_data == {
        "api_token": "new-token",
        "public_name": "并发更新值",
    }
