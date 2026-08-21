"""TSK-221：ConfigManager 快照 listener 发布语义测试。

覆盖 listener 生命周期与发布时序：

- watcher 接纳更高 revision 后同步调用 listener，callback 内读取的当前
  缓存快照必须恰好等于 callback 参数；
- 相同 / 较低 revision 不重复发布；
- 注销后停止回调，但快照仍继续推进；
- 本地 strict CAS 写入与既有 ``update_field_async`` 写入都汇聚到同一
  发布路径，且发布先于写入调用返回。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import BaseModel

from komari_bot.plugins.config_manager import manager as manager_module
from komari_bot.plugins.config_manager.manager import ConfigManager
from komari_bot.plugins.config_manager.storage import StoredConfig

if TYPE_CHECKING:
    from collections.abc import Callable

_UPDATED_AT_BASE = datetime(2026, 8, 8, 8, 0, 0, tzinfo=UTC)


class _MarkerConfig(BaseModel):
    marker: int = 0


def _stored(revision: int, marker: int | None = None) -> StoredConfig:
    return StoredConfig(
        plugin_name="listener-test",
        config_data={"marker": revision * 10 if marker is None else marker},
        revision=revision,
        updated_at=_UPDATED_AT_BASE + timedelta(seconds=revision),
    )


class _WatcherStorage:
    """模拟配置存储：提供初始快照并捕获 watcher 投递回调。"""

    def __init__(self, initial: StoredConfig) -> None:
        self._initial = initial
        self.watcher_callback: Callable[[StoredConfig], None] | None = None

    def register_watcher(
        self,
        _plugin_name: str,
        callback: Callable[[StoredConfig], None],
        *,
        max_staleness_seconds: float,
    ) -> None:
        del max_staleness_seconds
        self.watcher_callback = callback

    def fetch(self, _plugin_name: str) -> StoredConfig:
        return self._initial

    async def fetch_async(self, _plugin_name: str) -> StoredConfig:
        return self._initial

    def deliver(self, stored: StoredConfig) -> None:
        assert self.watcher_callback is not None, "manager 未注册快照 watcher"
        self.watcher_callback(stored)


class _CasStorage:
    """模拟配置存储：记录 CAS 调用并按序返回预置结果。"""

    def __init__(
        self,
        initial: StoredConfig,
        update_results: list[StoredConfig | None],
    ) -> None:
        self._initial = initial
        self._update_results = list(update_results)
        self.update_calls: list[dict[str, Any]] = []
        self.fetch_calls = 0
        self.watcher_callback: Callable[[StoredConfig], None] | None = None

    def register_watcher(
        self,
        _plugin_name: str,
        callback: Callable[[StoredConfig], None],
        *,
        max_staleness_seconds: float,
    ) -> None:
        del max_staleness_seconds
        self.watcher_callback = callback

    def fetch(self, _plugin_name: str) -> StoredConfig:
        self.fetch_calls += 1
        return self._initial

    async def fetch_async(self, _plugin_name: str) -> StoredConfig:
        self.fetch_calls += 1
        return self._initial

    async def update_fields_if_revision_async(
        self,
        **kwargs: Any,
    ) -> StoredConfig | None:
        self.update_calls.append(dict(kwargs))
        return self._update_results.pop(0)


def test_listener_observes_accepted_snapshot_synchronously(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _WatcherStorage(_stored(1))
    monkeypatch.setattr(manager_module, "get_config_storage", lambda: storage)
    manager = ConfigManager("listener-sync", _MarkerConfig)
    manager.initialize()

    publications: list[tuple[Any, Any]] = []

    def _listener(snapshot: Any) -> None:
        publications.append((snapshot, manager.get_cached_versioned_snapshot()))

    manager.register_snapshot_listener(_listener)
    storage.deliver(_stored(2))

    assert len(publications) == 1
    argument, current = publications[0]
    assert argument.revision == 2
    assert current == argument


def test_same_or_lower_revision_does_not_republish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _WatcherStorage(_stored(2))
    monkeypatch.setattr(manager_module, "get_config_storage", lambda: storage)
    manager = ConfigManager("listener-dedupe", _MarkerConfig)
    manager.initialize()

    observed: list[Any] = []
    manager.register_snapshot_listener(observed.append)

    storage.deliver(_stored(3))
    storage.deliver(_stored(3, marker=999))
    storage.deliver(_stored(2))

    assert len(observed) == 1
    snapshot = manager.get_cached_versioned_snapshot()
    assert snapshot.revision == 3
    assert snapshot.value.marker == 30


def test_unregister_stops_listener_but_snapshot_still_advances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _WatcherStorage(_stored(1))
    monkeypatch.setattr(manager_module, "get_config_storage", lambda: storage)
    manager = ConfigManager("listener-unregister", _MarkerConfig)
    manager.initialize()

    observed: list[Any] = []

    def _listener(snapshot: Any) -> None:
        observed.append(snapshot)

    manager.register_snapshot_listener(_listener)
    storage.deliver(_stored(2))
    assert len(observed) == 1

    manager.unregister_snapshot_listener(_listener)
    storage.deliver(_stored(3))

    assert len(observed) == 1
    assert manager.get_cached_versioned_snapshot().revision == 3


@pytest.mark.asyncio
async def test_local_strict_cas_publishes_snapshot_before_returning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _CasStorage(_stored(1), [_stored(2)])
    monkeypatch.setattr(manager_module, "get_config_storage", lambda: storage)
    manager = ConfigManager("listener-local-write", _MarkerConfig)
    await manager.initialize_async()

    timeline: list[str] = []
    observed: list[Any] = []

    def _listener(snapshot: Any) -> None:
        observed.append(snapshot)
        timeline.append("listener")

    manager.register_snapshot_listener(_listener)
    result = await manager.update_field_if_revision_async(
        "marker", 20, expected_revision=1
    )
    timeline.append("returned")

    assert timeline == ["listener", "returned"]
    assert result is not None
    assert len(observed) == 1
    assert observed[0] == result
    assert observed[0] == manager.get_cached_versioned_snapshot()


@pytest.mark.asyncio
async def test_successive_local_writes_publish_snapshots_in_revision_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _CasStorage(_stored(1), [_stored(2), _stored(3)])
    monkeypatch.setattr(manager_module, "get_config_storage", lambda: storage)
    manager = ConfigManager("listener-successive", _MarkerConfig)
    await manager.initialize_async()

    observed: list[Any] = []
    manager.register_snapshot_listener(observed.append)

    first = await manager.update_field_if_revision_async(
        "marker", 20, expected_revision=1
    )
    second = await manager.update_field_if_revision_async(
        "marker", 30, expected_revision=2
    )

    assert [item.revision for item in observed] == [2, 3]
    assert observed == [first, second]


@pytest.mark.asyncio
async def test_existing_update_entry_converges_to_same_listener_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _CasStorage(_stored(1), [_stored(2)])
    monkeypatch.setattr(manager_module, "get_config_storage", lambda: storage)
    manager = ConfigManager("listener-legacy-write", _MarkerConfig)
    await manager.initialize_async()

    observed: list[Any] = []
    manager.register_snapshot_listener(observed.append)

    await manager.update_field_async("marker", 20)

    assert len(observed) == 1
    assert observed[0].revision == 2
    assert observed[0] == manager.get_cached_versioned_snapshot()
