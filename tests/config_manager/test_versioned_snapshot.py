"""TSK-221：ConfigManager 版本化快照读取 seam 测试。

覆盖 `get_cached_versioned_snapshot()` 的公开契约：

- 原子包含 ``value / revision / updated_at`` 的不可变快照；
- 未初始化时通过公开异常 fail-fast，绝不触发存储 I/O；
- watcher 投递的较低 / 乱序 / 非法 revision 不回退当前快照；
- 并发读者只能观察完整旧快照或完整新快照，不得撕裂。
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import pytest
from pydantic import BaseModel

from komari_bot.plugins.config_manager import manager as manager_module
from komari_bot.plugins.config_manager.manager import ConfigManager
from komari_bot.plugins.config_manager.storage import StoredConfig

if TYPE_CHECKING:
    from collections.abc import Callable

_UPDATED_AT_BASE = datetime(2026, 8, 8, 8, 0, 0, tzinfo=UTC)

# 并发用例的全部同步等待都必须有界：实现永不推进时测试限时失败而非无限挂起
_BARRIER_TIMEOUT_SECONDS = 10.0
_PHASE_TIMEOUT_SECONDS = 10.0
_JOIN_TIMEOUT_SECONDS = 5.0


class _MarkerConfig(BaseModel):
    marker: int = 0


def _stored(revision: int, marker: int | None = None) -> StoredConfig:
    """构造 marker 与 revision 绑定的存储快照，便于检测撕裂读取。"""
    return StoredConfig(
        plugin_name="snapshot-test",
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


def test_versioned_snapshot_fails_fast_before_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _forbidden_storage() -> None:
        raise AssertionError("未初始化的快照读取不得访问存储")

    monkeypatch.setattr(manager_module, "get_config_storage", _forbidden_storage)
    manager = ConfigManager("snapshot-uninitialized", _MarkerConfig)

    with pytest.raises(RuntimeError):
        manager.get_cached_versioned_snapshot()


def test_versioned_snapshot_exposes_value_revision_and_updated_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _WatcherStorage(_stored(4))
    monkeypatch.setattr(manager_module, "get_config_storage", lambda: storage)
    manager = ConfigManager("snapshot-read", _MarkerConfig)
    manager.initialize()

    snapshot = manager.get_cached_versioned_snapshot()

    value = cast("_MarkerConfig", snapshot.value)
    assert isinstance(value, _MarkerConfig)
    assert value.marker == 40
    assert snapshot.revision == 4
    assert snapshot.updated_at == _UPDATED_AT_BASE + timedelta(seconds=4)


def test_versioned_snapshot_is_immutable(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = _WatcherStorage(_stored(1))
    monkeypatch.setattr(manager_module, "get_config_storage", lambda: storage)
    manager = ConfigManager("snapshot-immutable", _MarkerConfig)
    manager.initialize()

    snapshot = manager.get_cached_versioned_snapshot()

    for field_name in ("value", "revision", "updated_at"):
        with pytest.raises((AttributeError, TypeError, ValueError)):
            setattr(snapshot, field_name, None)


def test_lower_or_out_of_order_revisions_never_roll_back_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _WatcherStorage(_stored(3))
    monkeypatch.setattr(manager_module, "get_config_storage", lambda: storage)
    manager = ConfigManager("snapshot-order", _MarkerConfig)
    manager.initialize()

    storage.deliver(_stored(5))
    storage.deliver(_stored(4))
    storage.deliver(_stored(2))

    snapshot = manager.get_cached_versioned_snapshot()
    assert snapshot.revision == 5
    assert cast("_MarkerConfig", snapshot.value).marker == 50


def test_invalid_higher_revision_delivery_leaves_snapshot_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _WatcherStorage(_stored(2))
    monkeypatch.setattr(manager_module, "get_config_storage", lambda: storage)
    manager = ConfigManager("snapshot-invalid", _MarkerConfig)
    manager.initialize()

    storage.deliver(
        StoredConfig(
            plugin_name="snapshot-test",
            config_data={"marker": "非法值"},
            revision=9,
            updated_at=_UPDATED_AT_BASE + timedelta(seconds=9),
        )
    )

    snapshot = manager.get_cached_versioned_snapshot()
    assert snapshot.revision == 2
    assert cast("_MarkerConfig", snapshot.value).marker == 20


def test_concurrent_readers_never_observe_a_torn_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并发读取不得撕裂：确定性分阶段同步，无 sleep。

    阶段 0：Barrier 保证全部读线程已启动并进入读取循环；
    阶段 1：读者先确认观察到旧快照（revision 1），证明读取与投递窗口重叠；
    阶段 2：writer 投递更高修订，读者持续并发读取；
    阶段 3：至少一个最终修订被读者确认观察到后才停止。
    所有 Barrier/Condition 等待均带 timeout，实现永不推进修订时限时失败；
    finally 中 join 后断言全部读线程已退出，不泄漏后台线程；
    读线程异常统一捕获汇总，在测试线程断言。
    """
    storage = _WatcherStorage(_stored(1))
    monkeypatch.setattr(manager_module, "get_config_storage", lambda: storage)
    manager = ConfigManager("snapshot-tearing", _MarkerConfig)
    manager.initialize()

    reader_count = 4
    final_revision = 201

    start_barrier = threading.Barrier(reader_count + 1)
    state_lock = threading.Lock()
    state_changed = threading.Condition(state_lock)
    stop = threading.Event()
    observed_revisions: list[int] = []
    violations: list[tuple[int, Any]] = []
    reader_errors: list[BaseException] = []

    def _reader() -> None:
        try:
            start_barrier.wait(timeout=_BARRIER_TIMEOUT_SECONDS)
            while not stop.is_set():
                snapshot = manager.get_cached_versioned_snapshot()
                value = cast("_MarkerConfig", snapshot.value)
                with state_changed:
                    observed_revisions.append(int(snapshot.revision))
                    if value.marker != snapshot.revision * 10:
                        violations.append((snapshot.revision, value.marker))
                    state_changed.notify_all()
        except BaseException as exc:
            with state_changed:
                reader_errors.append(exc)
                stop.set()
                state_changed.notify_all()

    threads = [threading.Thread(target=_reader) for _ in range(reader_count)]
    for thread in threads:
        thread.start()
    try:
        # 阶段 0/1：确认读者已启动并观察到旧快照
        start_barrier.wait(timeout=_BARRIER_TIMEOUT_SECONDS)
        with state_changed:
            initial_observed = state_changed.wait_for(
                lambda: bool(reader_errors) or 1 in observed_revisions,
                timeout=_PHASE_TIMEOUT_SECONDS,
            )
        assert not reader_errors, f"读线程异常: {reader_errors!r}"
        assert initial_observed, "读者未在限时内观察到初始快照（revision=1）"

        # 阶段 2：投递更高修订，读者并发读取
        for revision in range(2, final_revision + 1):
            storage.deliver(_stored(revision))

        # 阶段 3：等待至少一个读者确认观察到最终修订
        with state_changed:
            final_observed = state_changed.wait_for(
                lambda: bool(reader_errors)
                or max(observed_revisions, default=0) >= final_revision,
                timeout=_PHASE_TIMEOUT_SECONDS,
            )
        assert not reader_errors, f"读线程异常: {reader_errors!r}"
        assert final_observed, (
            f"读者未在限时内观察到最终修订（revision={final_revision}）"
        )
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=_JOIN_TIMEOUT_SECONDS)
        leaked_threads = [thread for thread in threads if thread.is_alive()]
        assert not leaked_threads, f"读线程未退出: {leaked_threads!r}"

    assert not reader_errors, f"读线程异常: {reader_errors!r}"
    assert violations == []
    assert observed_revisions, "读者线程未观察到任何快照"
    assert min(observed_revisions) == 1
    assert max(observed_revisions) == final_revision
    assert manager.get_cached_versioned_snapshot().revision == final_revision
