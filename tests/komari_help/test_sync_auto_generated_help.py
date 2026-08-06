"""Komari Help 自动同步逻辑测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest

from komari_bot.plugins.komari_help.engine import HelpEngine
from komari_bot.plugins.komari_help.scanner import (
    HelpScanAlreadyRunningError,
    scan_and_sync,
)


class _FakeScanLeaseEngine:
    index_rebuild_count: int
    scan_lease_owner: str | None = None

    async def acquire_scan_lease(
        self,
        owner_token: str,
        *,
        lease_seconds: int,
    ) -> bool:
        assert owner_token
        assert lease_seconds > 0
        self.scan_lease_owner = owner_token
        return True

    async def renew_scan_lease(
        self,
        owner_token: str,
        *,
        lease_seconds: int,
    ) -> bool:
        assert owner_token == self.scan_lease_owner
        assert lease_seconds > 0
        return True

    async def release_scan_lease(self, owner_token: str) -> None:
        assert owner_token == self.scan_lease_owner
        self.scan_lease_owner = None

    async def rebuild_keyword_index(self) -> None:
        self.index_rebuild_count += 1


def test_scan_and_sync_rebuilds_keyword_index_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugins = [
        SimpleNamespace(
            name="demo_plugin",
            metadata=SimpleNamespace(
                name="演示插件",
                description="提供演示命令",
                usage="/demo help",
            ),
        ),
        SimpleNamespace(
            name="quiet_plugin",
            metadata=SimpleNamespace(
                name="静默插件",
                description="不会发生变化",
                usage="/quiet help",
            ),
        ),
    ]

    class _FakeEngine(_FakeScanLeaseEngine):
        def __init__(self) -> None:
            self.sync_calls: list[dict[str, object]] = []
            self.index_rebuild_count = 0
            self.delete_calls: list[tuple[set[str], bool]] = []

        async def delete_auto_generated_help_by_plugins(
            self,
            plugin_names: set[str],
            *,
            rebuild_index: bool = True,
        ) -> int:
            self.delete_calls.append((plugin_names, rebuild_index))
            return 0

        async def sync_auto_generated_help(self, **kwargs: object) -> bool:
            self.sync_calls.append(kwargs)
            return kwargs["plugin_name"] == "demo_plugin"

        async def _build_keyword_index(self) -> None:
            self.index_rebuild_count += 1

    monkeypatch.setattr(
        "komari_bot.plugins.komari_help.scanner.get_loaded_plugins",
        lambda: plugins,
    )
    engine = _FakeEngine()

    updated_count = asyncio.run(scan_and_sync(cast("HelpEngine", engine)))

    assert updated_count == 1
    assert engine.delete_calls == [(set(), False)]
    assert [call["rebuild_index"] for call in engine.sync_calls] == [False, False]
    assert engine.index_rebuild_count == 1


def test_scan_and_sync_skips_disabled_plugins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugins = [
        SimpleNamespace(
            name="demo_plugin",
            metadata=SimpleNamespace(
                name="演示插件",
                description="提供演示命令",
                usage="/demo help",
            ),
        ),
        SimpleNamespace(
            name="enabled_plugin",
            metadata=SimpleNamespace(
                name="可用插件",
                description="仍会被同步",
                usage="/enabled help",
            ),
        ),
    ]

    class _FakeEngine(_FakeScanLeaseEngine):
        def __init__(self) -> None:
            self.sync_calls: list[dict[str, object]] = []
            self.index_rebuild_count = 0
            self.delete_calls: list[tuple[set[str], bool]] = []

        async def delete_auto_generated_help_by_plugins(
            self,
            plugin_names: set[str],
            *,
            rebuild_index: bool = True,
        ) -> int:
            self.delete_calls.append((plugin_names, rebuild_index))
            return 1 if "demo_plugin" in plugin_names else 0

        async def sync_auto_generated_help(self, **kwargs: object) -> bool:
            self.sync_calls.append(kwargs)
            return True

        async def _build_keyword_index(self) -> None:
            self.index_rebuild_count += 1

    monkeypatch.setattr(
        "komari_bot.plugins.komari_help.scanner.get_loaded_plugins",
        lambda: plugins,
    )
    monkeypatch.setattr(
        "komari_bot.plugins.komari_help.scanner.get_disabled_auto_help_plugins",
        lambda: {"demo_plugin"},
    )
    engine = _FakeEngine()

    updated_count = asyncio.run(scan_and_sync(cast("HelpEngine", engine)))

    assert updated_count == 1
    assert engine.delete_calls == [({"demo_plugin"}, False)]
    assert [call["plugin_name"] for call in engine.sync_calls] == ["enabled_plugin"]
    assert engine.index_rebuild_count == 1


def test_sync_auto_generated_help_returns_false_for_disabled_plugin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = HelpEngine()

    monkeypatch.setattr(
        "komari_bot.plugins.komari_help.engine.get_disabled_auto_help_plugins",
        lambda: {"demo_plugin"},
    )

    changed = asyncio.run(
        engine.sync_auto_generated_help(
            plugin_name="demo_plugin",
            title="演示插件",
            content="/demo help",
            keywords=["演示", "帮助"],
        )
    )

    assert changed is False


def test_scan_and_sync_rebuilds_index_when_only_disabled_cleanup_happens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeEngine(_FakeScanLeaseEngine):
        def __init__(self) -> None:
            self.index_rebuild_count = 0
            self.delete_calls: list[tuple[set[str], bool]] = []

        async def delete_auto_generated_help_by_plugins(
            self,
            plugin_names: set[str],
            *,
            rebuild_index: bool = True,
        ) -> int:
            self.delete_calls.append((plugin_names, rebuild_index))
            return 2

        async def sync_auto_generated_help(self, **_kwargs: object) -> bool:
            raise AssertionError

        async def _build_keyword_index(self) -> None:
            self.index_rebuild_count += 1

    monkeypatch.setattr(
        "komari_bot.plugins.komari_help.scanner.get_loaded_plugins",
        list,
    )
    monkeypatch.setattr(
        "komari_bot.plugins.komari_help.scanner.get_disabled_auto_help_plugins",
        lambda: {"demo_plugin"},
    )
    engine = _FakeEngine()

    updated_count = asyncio.run(scan_and_sync(cast("HelpEngine", engine)))

    assert updated_count == 0
    assert engine.delete_calls == [({"demo_plugin"}, False)]
    assert engine.index_rebuild_count == 1


def test_scan_and_sync_rejects_second_worker_without_touching_data() -> None:
    class _BusyEngine(_FakeScanLeaseEngine):
        index_rebuild_count = 0
        data_accessed = False

        async def acquire_scan_lease(
            self,
            owner_token: str,
            *,
            lease_seconds: int,
        ) -> bool:
            assert owner_token
            assert lease_seconds > 0
            return False

        async def delete_auto_generated_help_by_plugins(
            self,
            _plugin_names: set[str],
            *,
            rebuild_index: bool = True,
        ) -> int:
            del rebuild_index
            self.data_accessed = True
            return 0

    engine = _BusyEngine()

    with pytest.raises(HelpScanAlreadyRunningError):
        asyncio.run(scan_and_sync(cast("HelpEngine", engine)))

    assert engine.data_accessed is False
