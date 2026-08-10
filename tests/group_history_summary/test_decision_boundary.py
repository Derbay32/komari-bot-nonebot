"""group_history_summary 对判定插件的窄引用边界测试。

验收目标：群总结只经「require() 依赖声明 + 顶层 import」获得专用 operation，
不再持有宽重排服务、场景键、分数或阈值。
"""

from __future__ import annotations

import inspect

import komari_bot.onebot as onebot_boundary
import komari_bot.plugins.group_history_summary as summary_module
import komari_bot.plugins.komari_decision as decision_plugin


def test_summary_operation_is_top_level_import_identity() -> None:
    assert (
        summary_module.classify_summary_request
        is decision_plugin.classify_summary_request
    )


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
