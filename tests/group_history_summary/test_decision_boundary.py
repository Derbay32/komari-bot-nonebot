"""group_history_summary 对判定插件的引用边界测试（ticket #32）。

验收目标：统一候选重排服务经「require() 依赖声明 + 顶层 import」获得，
不再通过 require 返回模块的属性访问引用，恢复完整类型信息。
"""

from __future__ import annotations

import inspect

import komari_bot.plugins.group_history_summary as summary_module
import komari_bot.plugins.komari_decision as decision_plugin


def test_rerank_service_is_top_level_import_identity() -> None:
    assert (
        summary_module.UnifiedCandidateRerankService
        is decision_plugin.UnifiedCandidateRerankService
    )
    assert isinstance(
        summary_module._scene_rerank_service,
        decision_plugin.UnifiedCandidateRerankService,
    )


def test_no_attribute_access_reference_to_decision_module() -> None:
    """入口模块不再保留 require 返回模块的属性访问式引用。"""
    source = inspect.getsource(summary_module)
    assert "komari_decision_plugin" not in source


def test_decision_dependency_declaration_kept() -> None:
    """require() 依赖声明仍然保留。"""
    source = inspect.getsource(summary_module)
    assert 'require("komari_decision")' in source
