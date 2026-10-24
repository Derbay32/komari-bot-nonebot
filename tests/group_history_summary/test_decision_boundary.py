"""group_history_summary 对判定插件的窄引用边界测试。

验收目标：群总结只经「require() 依赖声明 + 顶层 import」获得共享载体与唯一
引擎 getter，不再持有宽重排服务、场景键、分数、阈值或旧归类 operation。
"""

from __future__ import annotations

import inspect

import komari_bot.onebot as onebot_boundary
import komari_bot.plugins.group_history_summary as summary_module
import komari_bot.plugins.komari_decision as decision_plugin


def test_summary_dispatch_surface_is_top_level_import_identity() -> None:
    assert (
        summary_module.get_decision_dispatch
        is decision_plugin.get_decision_dispatch
    )
    assert summary_module.get_decision_engine is decision_plugin.get_decision_engine


def test_summary_entry_no_longer_uses_legacy_classifier_operation() -> None:
    """总结入口不再调用旧归类 operation，也不携带旧归类结果类型。"""
    source = inspect.getsource(summary_module)
    assert "classify_summary_request" not in source
    assert "SummaryRequestClassification" not in source


def test_failure_notification_uses_shared_onebot_boundary() -> None:
    assert (
        summary_module.GroupTaskFailureNotification
        is onebot_boundary.GroupTaskFailureNotification
    )
    assert (
        summary_module.GroupTaskFailureNotifier
        is onebot_boundary.GroupTaskFailureNotifier
    )


def test_no_attribute_access_reference_to_decision_module() -> None:
    """入口模块不再保留 require 返回模块的属性访问式引用。"""
    source = inspect.getsource(summary_module)
    assert "komari_decision_plugin" not in source


def test_entry_does_not_know_wide_rerank_internals() -> None:
    source = inspect.getsource(summary_module)
    for forbidden in (
        "UnifiedCandidateRerankService",
        "_scene_rerank_service",
        "best_scene_id",
        "best_scene_score",
        "meaningful_score",
        "noise_score",
        "SUMMARY_SCENE_ID",
    ):
        assert forbidden not in source


def test_decision_dependency_declaration_kept() -> None:
    """require() 依赖声明仍然保留。"""
    source = inspect.getsource(summary_module)
    assert 'require("komari_decision")' in source


def test_failure_notification_reads_memory_switch_through_top_level_export() -> None:
    """失败通知开关经 komari_memory 顶层暴露面 + 既有 config_manager factory。"""
    import komari_bot.plugins.komari_memory as memory_plugin

    assert (
        summary_module.KomariMemoryConfigSchema
        is memory_plugin.KomariMemoryConfigSchema
    )
    source = inspect.getsource(summary_module)
    require_line = 'require("komari_memory")'
    import_line = (
        "from komari_bot.plugins.komari_memory import KomariMemoryConfigSchema"
    )
    assert require_line in source
    assert import_line in source
    assert source.index(require_line) < source.index(import_line)
    assert (
        "_memory_config_manager = config_manager_plugin.get_config_manager("
        in source
    )


def test_legacy_scene_classification_notification_wrappers_are_removed() -> None:
    """旧的场景归类通知包装被删除，失败通知只在判定边界一次性提交。"""
    source = inspect.getsource(summary_module)
    assert "def _classification_notification" not in source
    assert "def _notify_classification_failure" not in source
    assert 'stage="scene_classification"' not in source
    assert 'claim_handoff("failure_notification")' in source
    assert "reaction_sent" not in source
