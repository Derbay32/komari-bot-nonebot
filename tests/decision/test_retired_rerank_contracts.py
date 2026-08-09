"""旧宽重排服务与跨插件契约的删除性验收测试。"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import komari_bot.plugins.komari_decision as decision_plugin
import komari_bot.plugins.komari_decision.services as decision_services
from komari_bot import decision as decision_contracts

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_ROOT = PROJECT_ROOT / "komari_bot"

RETIRED_EXPORTS = {
    "CandidateSchema",
    "SceneRuntimeUnavailableError",
    "UnifiedCandidateRerankService",
    "UnifiedRerankResult",
}

RETIRED_SOURCE_MARKERS = RETIRED_EXPORTS | {"unified_candidate_rerank"}


def test_retired_wide_symbols_are_absent_from_public_surfaces() -> None:
    """共享包、插件顶层与 services 包均不得保留兼容导出。"""
    public_surfaces = (
        decision_contracts,
        decision_plugin,
        decision_services,
    )

    for public_surface in public_surfaces:
        exported = set(public_surface.__all__)
        assert RETIRED_EXPORTS.isdisjoint(exported)
        for symbol in RETIRED_EXPORTS:
            assert not hasattr(public_surface, symbol)


def test_retired_wide_modules_are_deleted() -> None:
    """旧实现与共享宽契约文件必须物理删除，不能保留 shim。"""
    retired_modules = (
        PRODUCTION_ROOT / "decision" / "unified_candidate_rerank.py",
        PRODUCTION_ROOT
        / "plugins"
        / "komari_decision"
        / "services"
        / "unified_candidate_rerank.py",
    )

    assert not [path for path in retired_modules if path.exists()]


def test_decision_outcome_does_not_expose_internal_rank_result() -> None:
    """聊天调用方只接收稳定标量，不得观察深 module 的内部结果。"""
    field_names = {
        field.name for field in dataclasses.fields(decision_contracts.DecisionOutcome)
    }

    assert "rank_result" not in field_names


def test_production_code_has_no_retired_wide_contract_reference() -> None:
    """生产代码不得复活旧标识、模块路径或兼容 facade。"""
    offenders = {
        str(module_file.relative_to(PROJECT_ROOT)): sorted(
            marker
            for marker in RETIRED_SOURCE_MARKERS
            if marker in module_file.read_text(encoding="utf-8")
        )
        for module_file in sorted(PRODUCTION_ROOT.rglob("*.py"))
        if any(
            marker in module_file.read_text(encoding="utf-8")
            for marker in RETIRED_SOURCE_MARKERS
        )
    }

    assert not offenders, f"生产代码仍引用旧宽重排契约: {offenders}"
