"""配置管理器注册表与锁粒度测试。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import TYPE_CHECKING, cast

import pytest
from pydantic import BaseModel

from komari_bot.plugins.config_manager import manager as manager_module
from komari_bot.plugins.config_manager.manager import (
    ConfigManager,
    get_config_manager,
)
from komari_bot.plugins.config_manager.storage import StoredConfig

if TYPE_CHECKING:
    from collections.abc import Iterator


class _ConfigSchema(BaseModel):
    value: int = 1


class _ConflictingConfigSchema(BaseModel):
    enabled: bool = True


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
            schema_name="_ConfigSchema",
            config_data={"value": 1},
            version="1.0",
            revision=1,
            updated_at=datetime.now().astimezone(),
        )


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
