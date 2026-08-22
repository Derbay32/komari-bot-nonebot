"""group_history_summary 服务测试公共 fixture。

TSK-191 起 Prompt 无 Python 默认正文：非 loader-contract 的服务测试不得
隐式依赖 DEFAULTS（无 DB 时公开 loader 冷启动失败）。本 conftest 为只测试
其他业务的用例经公开 loader seam（``prompt_template.get_template`` 及其
消费方直接引用）注入完整的 marker Prompt 快照，字段集合来自强类型 Schema
oracle（``tests.config.prompt_field_contract``），不触碰模块级 loader 缓存，
避免测试顺序导致的 cache 泄漏。

只作用于本目录：loader 冷启动/cache 契约测试（``tests/config/
test_prompt_loader_contract.py`` 等）保持真实加载路径，不受本 fixture 影响。
"""

from __future__ import annotations

import pytest

from tests.config.prompt_field_contract import prompt_marker_values

_RESOURCE_ID = "group_history_summary"


@pytest.fixture(autouse=True)
def _inject_marker_prompt_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """为依赖总结/规划 Prompt 的服务测试注入完整 marker 快照。"""
    import komari_bot.plugins.group_history_summary.planner_service as planner_module
    import komari_bot.plugins.group_history_summary.prompt_template as seam_module
    import komari_bot.plugins.group_history_summary.summarize_service as summarize_module

    template = prompt_marker_values(_RESOURCE_ID)

    async def _marker_template() -> dict[str, str]:
        return dict(template)

    # 公开 loader seam 与其消费方直接引用（后者为既有加载路径）
    monkeypatch.setattr(seam_module, "get_template", _marker_template)
    monkeypatch.setattr(planner_module, "get_template", _marker_template)
    monkeypatch.setattr(summarize_module, "get_template", _marker_template)


@pytest.fixture(autouse=True)
def _group_history_summary_default_admit(monkeypatch: pytest.MonkeyPatch) -> None:
    """TSK-229：既有总结逻辑测试的准入默认门。

    群历史总结编排层现已在每个瞬时效果前接入 ``group_admission`` 顶层
    ``adjudicate`` 裁决（ADR-0012）。既有服务测试（test_planner_service /
    test_summarize_service / test_execution_service 等）关注总结内部逻辑而非
    准入验收，默认策略应视为「获准开展业务」。本 fixture 为该目录全部用例注入
    一个 per-test「默认放行」顶替 ``adjudicate``（BUSINESS 获准）+ READY 运行
    时可读面。

    注意：tests/group_admission 的准入红/绿基线自带 ScriptedAdjudicate 控制，
    不受本默认门影响。
    """
    import komari_bot.plugins.group_admission as admission_pkg
    from komari_bot.plugins.group_admission.contracts import (
        AdmissionQualification,
        AdmissionResult,
    )
    from tests.group_admission.chat_admission_support import _stub_runtime_state

    def _admit(*_args: object, **_kwargs: object) -> AdmissionResult:
        del _args, _kwargs
        return AdmissionResult(
            qualification=AdmissionQualification.BUSINESS,
            effective_revision=1,
            reason_code="policy_admitted",
        )

    monkeypatch.setattr(admission_pkg, "adjudicate", _admit)
    monkeypatch.setattr(admission_pkg, "get_runtime_state", _stub_runtime_state)
