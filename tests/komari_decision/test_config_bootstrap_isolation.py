"""TSK-182：判定插件配置读取失败的启动隔离与公共健康态投影。

全部用例使用真实 ``ConfigManager`` 与真实 ``KomariDecisionConfigSchema``，只
替换外部存储边界；scene 子系统构造点全部 fail-if-called，确保配置失败时不会
启动任何场景初始化，也不会触发 SDK / embedding / 网络调用。
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest
from loguru import logger as loguru_logger
from pydantic import ValidationError

import komari_bot.plugins.komari_decision as decision_plugin
from komari_bot.plugins.config_manager import manager as manager_module
from komari_bot.plugins.config_manager.manager import ConfigManager
from komari_bot.plugins.config_manager.storage import StoredConfig
from komari_bot.plugins.komari_decision.config_schema import (
    KomariDecisionConfigSchema,
)
from komari_bot.plugins.komari_decision.services import config_interface

if TYPE_CHECKING:
    from collections.abc import Iterator

_UPDATED_AT = datetime(2026, 10, 24, 11, 0, 0, tzinfo=UTC)
_SENTINEL = "SENTINEL-TSK182-c0ffee"
_EXPECTED_FAILED_REASON = "判定配置暂不可用"

_SCENE_SEAMS = (
    "komari_bot.plugins.komari_decision.repositories.scene_repository.SceneRepository",
    "komari_bot.plugins.komari_decision.services.scene_runtime_service.SceneRuntimeService",
    "komari_bot.plugins.komari_decision.services.scene_sync_service.SceneSyncService",
    "komari_bot.plugins.komari_decision.services.scene_embedding_worker.SceneEmbeddingWorker",
    "komari_bot.plugins.komari_decision.services.scene_admin_service.SceneAdminService",
    "komari_bot.plugins.komari_decision.handlers.scene_sync_worker.register_scene_sync_task",
    "komari_bot.plugins.komari_decision.handlers.scene_sync_worker.bootstrap_scene_sync_task",
)
"""scene 子系统构造 / 启动入口：配置读取失败后一律不得被调用。"""


def _valid_data(**overrides: Any) -> dict[str, Any]:
    """用真实 Schema 生成一份完整、可校验通过的持久化数据。"""
    return KomariDecisionConfigSchema(**overrides).model_dump(mode="json")


def _stored(data: dict[str, Any], revision: int) -> StoredConfig:
    return StoredConfig(
        plugin_name="komari_decision",
        config_data=data,
        revision=revision,
        updated_at=_UPDATED_AT,
    )


class _FaultStorage:
    """只替换外部存储边界：读取结果或被注入的故障由用例决定。"""

    def __init__(self, entry: StoredConfig | BaseException) -> None:
        self.entry = entry
        self.fetch_calls = 0

    def register_watcher(
        self,
        _plugin_name: str,
        _callback: Any,
        *,
        max_staleness_seconds: float,
    ) -> None:
        del _plugin_name, _callback, max_staleness_seconds

    def _read(self, _plugin_name: str) -> StoredConfig:
        self.fetch_calls += 1
        if isinstance(self.entry, BaseException):
            raise self.entry
        return self.entry

    def fetch(self, plugin_name: str) -> StoredConfig:
        return self._read(plugin_name)

    async def fetch_async(self, plugin_name: str) -> StoredConfig:
        return self._read(plugin_name)

    def insert_if_absent(self, **_kwargs: Any) -> StoredConfig:
        raise AssertionError("配置读取失败后不得写入默认配置")

    async def insert_if_absent_async(self, **_kwargs: Any) -> StoredConfig:
        raise AssertionError("配置读取失败后不得写入默认配置")

    def update_if_unchanged(self, **_kwargs: Any) -> StoredConfig | None:
        raise AssertionError("本用例不应触发配置归一化写回")

    async def update_if_unchanged_async(self, **_kwargs: Any) -> StoredConfig | None:
        raise AssertionError("本用例不应触发配置归一化写回")


@contextmanager
def _capture_logs() -> Iterator[list[str]]:
    """捕获 loguru 日志（``nonebot.logger`` 就是 loguru 全局 logger）。"""
    messages: list[str] = []

    def _sink(message: Any) -> None:
        messages.append(str(message))

    handler_id = loguru_logger.add(_sink, level="DEBUG")
    try:
        yield messages
    finally:
        loguru_logger.remove(handler_id)


def _fail_if_called(name: str) -> Any:
    """构造一个只要被调用就失败的替身（场景初始化必须完全不发生）。"""
    message = f"配置读取失败后不得构造 {name}"

    def _factory(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError(message)

    return _factory


@pytest.fixture
def scene_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 scene 子系统全部入口替换为 fail-if-called。"""
    for path in _SCENE_SEAMS:
        monkeypatch.setattr(path, _fail_if_called(path))


@pytest.fixture
def decision_config_manager(monkeypatch: pytest.MonkeyPatch) -> ConfigManager:
    """用真实 ConfigManager + 真实 Schema 提供插件内的配置读取 seam。"""
    manager = ConfigManager("komari_decision", KomariDecisionConfigSchema)
    monkeypatch.setattr(config_interface, "_config_manager", manager)
    return manager


def _install_storage(
    monkeypatch: pytest.MonkeyPatch,
    storage: _FaultStorage,
) -> None:
    monkeypatch.setattr(manager_module, "get_config_storage", lambda: storage)


@pytest.mark.usefixtures("scene_seams", "decision_config_manager")
def test_initial_read_failure_is_isolated_and_projected_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """初始配置读取失败被隔离：不启动 scene，公共健康态如实投影 failed。"""
    storage = _FaultStorage(OSError(f"config source unavailable {_SENTINEL}"))
    _install_storage(monkeypatch, storage)
    manager = decision_plugin.PluginManager()

    with _capture_logs() as messages:
        asyncio.run(manager.initialize())
        logged_after_initialize = len(messages)
        first = manager.runtime_state
        second = manager.runtime_state
        assert len(messages) == logged_after_initialize

    assert first.status is decision_plugin.DecisionRuntimeStatus.FAILED
    assert second == first
    assert first.reason == _EXPECTED_FAILED_REASON
    assert _SENTINEL not in first.reason
    assert manager.scene_repository is None
    assert manager.scene_admin is None
    assert manager.scene_runtime is None
    assert manager.scene_sync is None
    assert manager.scene_embedding_worker is None
    joined = "".join(messages)
    assert _SENTINEL not in joined
    assert "OSError" in joined


@pytest.mark.usefixtures("scene_seams", "decision_config_manager")
def test_invalid_persisted_config_fails_health_without_default_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 Schema 校验失败同样被隔离，且配置读取接口仍拒绝非法输入。"""
    storage = _FaultStorage(_stored({"reply_threshold": 7.5}, 4))
    _install_storage(monkeypatch, storage)
    manager = decision_plugin.PluginManager()

    with _capture_logs() as messages:
        asyncio.run(manager.initialize())
        state = manager.runtime_state

    assert state.status is decision_plugin.DecisionRuntimeStatus.FAILED
    assert state.reason == _EXPECTED_FAILED_REASON
    assert manager.scene_runtime is None
    assert "ValidationError" in "".join(messages)

    # 失败态只是投影，绝不缓存默认值：真实配置接口仍然抛出校验错误。
    with pytest.raises(ValidationError):
        asyncio.run(config_interface.get_config_async())


@pytest.mark.parametrize(
    ("plugin_enable", "scene_persist_enabled"),
    [(False, True), (True, False)],
)
@pytest.mark.usefixtures("scene_seams", "decision_config_manager")
def test_disabled_path_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    *,
    plugin_enable: bool,
    scene_persist_enabled: bool,
) -> None:
    """既有 disabled 判定路径行为不变。"""
    storage = _FaultStorage(
        _stored(
            _valid_data(
                plugin_enable=plugin_enable,
                scene_persist_enabled=scene_persist_enabled,
            ),
            2,
        )
    )
    _install_storage(monkeypatch, storage)
    manager = decision_plugin.PluginManager()

    asyncio.run(manager.initialize())

    state = manager.runtime_state
    assert state.status is decision_plugin.DecisionRuntimeStatus.DISABLED
    assert manager.scene_runtime is None


@pytest.mark.usefixtures("scene_seams", "decision_config_manager")
def test_repaired_source_reaches_normal_path_on_later_initialize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """修复外部数据源后，后续显式初始化重新走正常判定路径。"""
    storage = _FaultStorage(_stored({"reply_threshold": 7.5}, 4))
    _install_storage(monkeypatch, storage)
    manager = decision_plugin.PluginManager()

    asyncio.run(manager.initialize())
    assert (
        manager.runtime_state.status is decision_plugin.DecisionRuntimeStatus.FAILED
    )

    storage.entry = _stored(_valid_data(plugin_enable=False), 5)
    reads_before_retry = storage.fetch_calls
    asyncio.run(manager.initialize())

    assert manager.runtime_state.status is decision_plugin.DecisionRuntimeStatus.DISABLED
    assert storage.fetch_calls == reads_before_retry + 1


@pytest.mark.usefixtures("scene_seams", "decision_config_manager")
def test_config_failure_reason_is_fixed_and_shared_by_both_observations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启动失败写入的运行态与健康观测返回的是同一条固定安全文案。"""
    storage = _FaultStorage(OSError(f"boom {_SENTINEL}"))
    _install_storage(monkeypatch, storage)
    manager = decision_plugin.PluginManager()

    asyncio.run(manager.initialize())
    projected = manager.runtime_state

    assert projected.reason == _EXPECTED_FAILED_REASON
    assert manager._runtime_state.reason == projected.reason
    assert _SENTINEL not in projected.reason
    assert cast("Any", manager._runtime_state).status is (
        decision_plugin.DecisionRuntimeStatus.FAILED
    )
