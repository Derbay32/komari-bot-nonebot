"""Komari Help 自动同步逻辑测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from komari_bot.plugins.komari_help.engine import HelpEngine
from komari_bot.plugins.komari_help.scanner import scan_and_sync

if TYPE_CHECKING:
    import pytest


class _FakeConn:
    def __init__(self, existing_row: dict[str, object] | None) -> None:
        self.existing_row = existing_row
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetchrow(self, query: str, *args: object) -> dict[str, object] | None:
        assert "SELECT id, is_auto_generated, title, content, keywords" in query
        assert args == ("demo_plugin",)
        return self.existing_row

    async def execute(self, query: str, *args: object) -> None:
        self.execute_calls.append((query, args))


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    def acquire(self) -> "_FakePool":
        return self

    async def __aenter__(self) -> _FakeConn:
        return self.conn

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb


class _FakeDeleteConn:
    def __init__(self, result: str) -> None:
        self.result = result
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []

    async def execute(self, query: str, *args: object) -> str:
        self.execute_calls.append((query, args))
        return self.result


class _FakeDeletePool:
    def __init__(self, conn: _FakeDeleteConn) -> None:
        self.conn = conn

    def acquire(self) -> "_FakeDeletePool":
        return self

    async def __aenter__(self) -> _FakeDeleteConn:
        return self.conn

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb


def test_sync_auto_generated_help_skips_unchanged_content() -> None:
    engine = HelpEngine()
    conn = _FakeConn(
        {
            "id": 1,
            "is_auto_generated": True,
            "title": "演示插件",
            "content": "/demo help",
            "keywords": ["帮助", "演示"],
        }
    )
    engine._pool = _FakePool(conn)

    async def _unexpected_get_embedding(_text: str) -> list[float]:
        raise AssertionError

    async def _unexpected_build_keyword_index() -> None:
        raise AssertionError

    engine._get_embedding = _unexpected_get_embedding  # type: ignore[method-assign]
    engine._build_keyword_index = _unexpected_build_keyword_index  # type: ignore[method-assign]

    changed = asyncio.run(
        engine.sync_auto_generated_help(
            plugin_name="demo_plugin",
            title="演示插件",
            content="/demo help",
            keywords=["演示", "帮助"],
        )
    )

    assert changed is False
    assert conn.execute_calls == []


def test_sync_auto_generated_help_updates_changed_content_without_rebuilding_index() -> (
    None
):
    engine = HelpEngine()
    conn = _FakeConn(
        {
            "id": 1,
            "is_auto_generated": True,
            "title": "旧标题",
            "content": "旧内容",
            "keywords": ["旧关键词"],
        }
    )
    engine._pool = _FakePool(conn)

    async def _fake_get_embedding(text: str) -> list[float]:
        assert text == "新标题\n新内容"
        return [0.1, 0.2]

    async def _unexpected_build_keyword_index() -> None:
        raise AssertionError

    engine._get_embedding = _fake_get_embedding  # type: ignore[method-assign]
    engine._build_keyword_index = _unexpected_build_keyword_index  # type: ignore[method-assign]

    changed = asyncio.run(
        engine.sync_auto_generated_help(
            plugin_name="demo_plugin",
            title="新标题",
            content="新内容",
            keywords=["新关键词"],
            rebuild_index=False,
        )
    )

    assert changed is True
    update_query, update_args = conn.execute_calls[0]
    assert "UPDATE komari_help" in update_query
    assert update_args == (
        1,
        "feature",
        ["新关键词"],
        "新标题",
        "新内容",
        None,
        str([0.1, 0.2]),
    )


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

    class _FakeEngine:
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

    class _FakeEngine:
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


def test_delete_auto_generated_help_by_plugins_only_removes_auto_generated() -> None:
    engine = HelpEngine()
    conn = _FakeDeleteConn("DELETE 2")
    engine._pool = _FakeDeletePool(conn)

    rebuilt = 0

    async def _fake_build_keyword_index() -> None:
        nonlocal rebuilt
        rebuilt += 1

    engine._build_keyword_index = _fake_build_keyword_index  # type: ignore[method-assign]

    deleted_count = asyncio.run(
        engine.delete_auto_generated_help_by_plugins(
            {"demo_plugin", "other_plugin"},
        )
    )

    assert deleted_count == 2
    delete_query, delete_args = conn.execute_calls[0]
    assert "DELETE FROM komari_help" in delete_query
    assert "is_auto_generated = TRUE" in delete_query
    assert delete_args == (["demo_plugin", "other_plugin"],)
    assert rebuilt == 1


def test_scan_and_sync_rebuilds_index_when_only_disabled_cleanup_happens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeEngine:
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
