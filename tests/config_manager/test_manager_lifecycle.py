"""配置管理器注册表与锁粒度测试。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

import pytest
from pydantic import BaseModel

from komari_bot.plugins.config_manager import manager as manager_module
from komari_bot.plugins.config_manager.manager import (
    ConfigManager,
    get_config_manager,
    initialize_registered_config_managers_async,
)
from komari_bot.plugins.config_manager.storage import StoredConfig

if TYPE_CHECKING:
    from collections.abc import Iterator


class _ConfigSchema(BaseModel):
    value: int = 1


class _ConflictingConfigSchema(BaseModel):
    enabled: bool = True


class _EnvConfigSchema(_ConfigSchema):
    pass


@pytest.fixture(autouse=True)
def _clear_manager_registry() -> Iterator[None]:
    manager_module._config_managers.clear()
    yield
    manager_module._config_managers.clear()


def test_factory_is_the_only_singleton_boundary() -> None:
    first = get_config_manager("registry-test", _ConfigSchema)
    second = get_config_manager("registry-test", _ConfigSchema)
    independent = ConfigManager("registry-test", _ConfigSchema)

    assert first is second
    assert independent is not first
    assert len(manager_module._config_managers) == 1


def test_factory_rejects_two_schemas_for_the_same_storage_resource() -> None:
    get_config_manager("schema-conflict", _ConfigSchema)

    with pytest.raises(ValueError, match="已注册配置 Schema"):
        get_config_manager("schema-conflict", _ConflictingConfigSchema)


def test_manager_uses_dedicated_environment_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_schemas: list[type[BaseModel]] = []

    def _get_plugin_config(schema: type[BaseModel]) -> BaseModel:
        requested_schemas.append(schema)
        return schema(value=7)

    monkeypatch.setattr(manager_module, "get_plugin_config", _get_plugin_config)
    manager = ConfigManager(
        "dedicated-env-schema",
        _ConfigSchema,
        env_config_schema=_EnvConfigSchema,
    )

    config = manager._get_env_config()

    assert requested_schemas == [_EnvConfigSchema]
    assert isinstance(config, _EnvConfigSchema)
    assert config.value == 7


def test_factory_creation_is_thread_safe() -> None:
    with ThreadPoolExecutor(max_workers=8) as executor:
        managers = list(
            executor.map(
                lambda _index: get_config_manager("threaded-registry", _ConfigSchema),
                range(32),
            )
        )

    assert all(manager is managers[0] for manager in managers)
    assert len(manager_module._config_managers) == 1


class _ParallelFetchStorage:
    def __init__(self) -> None:
        self.barrier = threading.Barrier(2)

    def fetch(self, plugin_name: str) -> StoredConfig:
        self.barrier.wait(timeout=2)
        return StoredConfig(
            plugin_name=plugin_name,
            config_data={"value": 1},
            revision=1,
            updated_at=datetime.now().astimezone(),
        )


class _InitializationRaceStorage:
    def __init__(self) -> None:
        self.callback: object | None = None
        self.insert_calls = 0
        self.current = StoredConfig(
            plugin_name="initialization-race",
            config_data={"value": 99},
            revision=2,
            updated_at=datetime.now().astimezone(),
        )

    def register_watcher(
        self,
        _plugin_name: str,
        callback: object,
        *,
        max_staleness_seconds: float,
    ) -> None:
        assert max_staleness_seconds == 1.0
        self.callback = callback

    def fetch(self, _plugin_name: str) -> None:
        return None

    def insert_if_absent(self, **_kwargs: object) -> StoredConfig:
        self.insert_calls += 1
        return self.current


class _AsyncStartupStorage:
    def __init__(self) -> None:
        self.fetch_calls: list[str] = []

    def register_watcher(
        self,
        _plugin_name: str,
        _callback: object,
        *,
        max_staleness_seconds: float,
    ) -> None:
        assert max_staleness_seconds == 1.0

    async def fetch_async(self, plugin_name: str) -> StoredConfig:
        self.fetch_calls.append(plugin_name)
        value = 1 if plugin_name == "startup-first" else 2
        return StoredConfig(
            plugin_name=plugin_name,
            config_data={"value": value},
            revision=1,
            updated_at=datetime.now().astimezone(),
        )


def test_concurrent_initialization_never_overwrites_existing_database_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _InitializationRaceStorage()
    monkeypatch.setattr(manager_module, "get_config_storage", lambda: storage)
    monkeypatch.setattr(
        ConfigManager,
        "_get_env_config",
        lambda _self: _ConfigSchema(value=1),
    )
    manager = ConfigManager("initialization-race", _ConfigSchema)

    config = cast("_ConfigSchema", manager.initialize())

    assert config.value == 99
    assert storage.insert_calls == 1


def test_external_revision_notification_atomically_replaces_cached_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _InitializationRaceStorage()
    monkeypatch.setattr(manager_module, "get_config_storage", lambda: storage)
    manager = ConfigManager("initialization-race", _ConfigSchema)
    manager._cache_stored_config(storage.current)
    manager.get()

    callback = cast("Any", storage.callback)
    callback(
        StoredConfig(
            plugin_name="initialization-race",
            config_data={"value": 100},
            revision=3,
            updated_at=datetime.now().astimezone(),
        )
    )

    assert cast("_ConfigSchema", manager.get()).value == 100


@pytest.mark.asyncio
async def test_startup_preheats_registered_managers_before_sync_consumers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _AsyncStartupStorage()
    monkeypatch.setattr(manager_module, "get_config_storage", lambda: storage)
    first = get_config_manager("startup-first", _ConfigSchema)
    second = get_config_manager("startup-second", _ConfigSchema)

    await initialize_registered_config_managers_async()

    assert storage.fetch_calls == ["startup-first", "startup-second"]
    assert cast("_ConfigSchema", first.get()).value == 1
    assert cast("_ConfigSchema", second.get()).value == 2


def test_sync_operations_for_different_plugins_do_not_share_a_global_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _ParallelFetchStorage()
    monkeypatch.setattr(manager_module, "get_config_storage", lambda: storage)
    first = ConfigManager("parallel-first", _ConfigSchema)
    second = ConfigManager("parallel-second", _ConfigSchema)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_result = executor.submit(first.initialize)
        second_result = executor.submit(second.initialize)

        assert cast("_ConfigSchema", first_result.result(timeout=3)).value == 1
        assert cast("_ConfigSchema", second_result.result(timeout=3)).value == 1
