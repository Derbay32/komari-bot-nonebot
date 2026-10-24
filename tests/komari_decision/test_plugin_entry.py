"""判定插件入口的引擎 getter 验收。

旧场景管理器（``PluginManager``）及其场景生命周期用例已随 TSK-189 物理退役。
本文件只保留当前 ``get_decision_engine`` 的运行时身份契约：它是唯一公共引擎
getter，只返回常驻运行时当前持有的真实引擎，不建立第二条构建/缓存路径。
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import komari_bot.plugins.komari_decision as decision_plugin


def _patch_runtime_engine(
    monkeypatch: Any,
    engine: object | None,
    *,
    closed: bool = False,
) -> Any:
    """把常驻运行时的当前引擎替换为受控身份（不构建第二条引擎路径）。"""
    runtime = decision_plugin.get_decision_runtime()
    monkeypatch.setattr(runtime, "_engine", engine)
    monkeypatch.setattr(runtime, "_closed", closed)
    return runtime


def test_get_decision_engine_returns_runtime_current_engine(
    monkeypatch: Any,
) -> None:
    engine = SimpleNamespace(name="real-engine")
    _patch_runtime_engine(monkeypatch, engine)

    assert decision_plugin.get_decision_engine() is engine
    # 同一运行时实例内重复读取返回同一身份（不是第二条构建/缓存路径）。
    assert decision_plugin.get_decision_engine() is engine


def test_get_decision_engine_returns_none_without_runtime_engine(
    monkeypatch: Any,
) -> None:
    _patch_runtime_engine(monkeypatch, None)

    assert decision_plugin.get_decision_engine() is None


def test_get_decision_engine_returns_none_after_runtime_closed(
    monkeypatch: Any,
) -> None:
    engine = SimpleNamespace(name="real-engine")
    _patch_runtime_engine(monkeypatch, engine, closed=True)

    assert decision_plugin.get_decision_engine() is None


def test_get_decision_engine_does_not_depend_on_memory_redis(
    monkeypatch: Any,
) -> None:
    """引擎 getter 只读常驻运行时；记忆 Redis 短时缺失不得改变真实引擎。"""
    engine = SimpleNamespace(name="real-engine")
    _patch_runtime_engine(monkeypatch, engine)

    memory_module = sys.modules["komari_bot.plugins.komari_memory"]
    monkeypatch.setattr(
        memory_module,
        "get_plugin_manager",
        lambda: SimpleNamespace(redis=None),
        raising=False,
    )

    assert decision_plugin.get_decision_engine() is engine


def test_runtime_getter_is_not_a_second_public_engine_getter() -> None:
    assert "get_decision_runtime" not in decision_plugin.__all__
    assert "get_decision_engine" in decision_plugin.__all__
