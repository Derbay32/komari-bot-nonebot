"""TSK-221：ConfigManager strict CAS 字段更新 seam 测试。

覆盖 ``update_field_if_revision_async()`` 契约：

- 成功时返回包含 ``value / revision / updated_at`` 的新快照，且按给定
  expected revision 恰好执行一次底层 CAS；
- 冲突时明确返回 ``None``：不自动读取数据库最新值、不重放覆盖、不发布；
- 无论成功或冲突，都不得经 ``fetch_async`` 重读存储。
"""

from __future__ import annotations

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


class _MarkerConfig(BaseModel):
    marker: int = 0


def _stored(revision: int, marker: int | None = None) -> StoredConfig:
    return StoredConfig(
        plugin_name="strict-cas-test",
        config_data={"marker": revision * 10 if marker is None else marker},
        revision=revision,
        updated_at=_UPDATED_AT_BASE + timedelta(seconds=revision),
    )


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


@pytest.mark.asyncio
async def test_strict_cas_success_returns_new_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _CasStorage(_stored(1), [_stored(2, marker=20)])
    monkeypatch.setattr(manager_module, "get_config_storage", lambda: storage)
    manager = ConfigManager("strict-cas-success", _MarkerConfig)
    await manager.initialize_async()

    result = await manager.update_field_if_revision_async(
        "marker", 20, expected_revision=1
    )

    assert result is not None
    assert result.revision == 2
    assert cast("_MarkerConfig", result.value).marker == 20
    assert result.updated_at == _UPDATED_AT_BASE + timedelta(seconds=2)


@pytest.mark.asyncio
async def test_strict_cas_success_issues_exactly_one_cas_without_reread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _CasStorage(_stored(1), [_stored(2, marker=20)])
    monkeypatch.setattr(manager_module, "get_config_storage", lambda: storage)
    manager = ConfigManager("strict-cas-single", _MarkerConfig)
    await manager.initialize_async()
    fetches_after_init = storage.fetch_calls

    result = await manager.update_field_if_revision_async(
        "marker", 20, expected_revision=1
    )

    assert result is not None
    assert len(storage.update_calls) == 1
    call = storage.update_calls[0]
    assert call["expected_revision"] == 1
    assert call["field_names"] == {"marker"}
    assert cast("_MarkerConfig", call["config"]).marker == 20
    assert storage.fetch_calls == fetches_after_init, "strict CAS 不得重读存储"


@pytest.mark.asyncio
async def test_strict_cas_conflict_returns_none_without_retry_or_reread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _CasStorage(_stored(1), [None])
    monkeypatch.setattr(manager_module, "get_config_storage", lambda: storage)
    manager = ConfigManager("strict-cas-conflict", _MarkerConfig)
    await manager.initialize_async()

    observed: list[Any] = []
    manager.register_snapshot_listener(observed.append)
    fetches_after_init = storage.fetch_calls

    result = await manager.update_field_if_revision_async(
        "marker", 999, expected_revision=1
    )

    assert result is None
    assert len(storage.update_calls) == 1, "冲突后不得自动重试 CAS"
    assert storage.fetch_calls == fetches_after_init, "冲突后不得读取最新值覆盖"
    assert observed == [], "冲突未接纳任何修订，不得发布"
    snapshot = manager.get_cached_versioned_snapshot()
    assert snapshot.revision == 1
    assert cast("_MarkerConfig", snapshot.value).marker == 10
