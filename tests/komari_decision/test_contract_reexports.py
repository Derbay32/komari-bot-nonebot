"""判定插件顶层 re-export 面保持不变验收测试（ticket #28）。

契约类型搬迁到 komari_bot.decision 共享包后，判定插件顶层导出的符号集合
必须与原集合完全一致，且契约符号与共享包中的对象为同一身份。
"""

from __future__ import annotations

import komari_bot.plugins.komari_decision as decision_plugin
from komari_bot import decision as contracts

EXPECTED_PLUGIN_ALL = {
    "CandidateSchema",
    "DecisionRuntimeState",
    "DecisionRuntimeStatus",
    "PluginManager",
    "UnifiedCandidateRerankService",
    "UnifiedRerankResult",
    "get_decision_engine",
    "get_plugin_manager",
    "get_runtime_state",
    "get_scene_admin_service",
}


def test_plugin_all_set_unchanged() -> None:
    assert set(decision_plugin.__all__) == EXPECTED_PLUGIN_ALL


def test_contract_symbols_are_shared_package_identities() -> None:
    assert decision_plugin.DecisionRuntimeState is contracts.DecisionRuntimeState
    assert decision_plugin.DecisionRuntimeStatus is contracts.DecisionRuntimeStatus
    assert decision_plugin.CandidateSchema is contracts.CandidateSchema
    assert decision_plugin.UnifiedRerankResult is contracts.UnifiedRerankResult


def test_engine_service_stays_plugin_owned() -> None:
    service = decision_plugin.UnifiedCandidateRerankService
    assert service.__module__.startswith("komari_bot.plugins.komari_decision")
