"""判定插件顶层暴露面验收：只保留当前 Jev 运行时的四个支持名称。

旧场景管理、旧运行时状态、旧总结归类 operation 与旧插件管理器均已随
TSK-189 物理退役；本文件断言顶层 ``__all__`` 与属性面不再提供任何兼容
别名，且新引擎 getter 仍由插件拥有。
"""

from __future__ import annotations

import komari_bot.plugins.komari_decision as decision_plugin

EXPECTED_PLUGIN_ALL = {
    "get_decision_dispatch",
    "get_decision_engine",
    "observe_delivered_reply",
    "register_decision_api",
}

RETIRED_NAMES = (
    "DecisionRuntimeState",
    "DecisionRuntimeStatus",
    "PluginManager",
    "_get_runtime_state",
    "classify_summary_request",
    "get_plugin_manager",
    "get_scene_admin_service",
    "get_runtime_state",
)


def test_plugin_all_matches_supported_surface() -> None:
    assert set(decision_plugin.__all__) == EXPECTED_PLUGIN_ALL


def test_retired_top_level_aliases_are_absent() -> None:
    for name in RETIRED_NAMES:
        assert name not in decision_plugin.__all__
        assert not hasattr(decision_plugin, name), f"顶层仍暴露旧别名 {name}"


def test_decision_engine_getter_remains_plugin_owned() -> None:
    assert decision_plugin.get_decision_engine.__module__ == (
        "komari_bot.plugins.komari_decision"
    )


def test_internal_runtime_getter_is_not_public_surface() -> None:
    """当前运行时 getter 供包内/测试使用，不是第二个公共引擎 getter。"""
    assert "get_decision_runtime" not in decision_plugin.__all__
