"""komari_debug 既有测试的准入默认门（TSK-229）。

komari_debug 的群公开诊断结果现已接入 ``group_admission`` 顶层
``adjudicate`` 裁决（ADR-0012）。既有 debug 测试（test_reporting /
test_commands 等）关注报告格式化与命令流程而非准入验收，默认策略应视为
「获准开展业务」。本 conftest 为该目录全部用例注入一个 per-test「默认放行」
顶替 ``adjudicate``（BUSINESS 获准）+ READY 运行时可读面。

注意：tests/group_admission 的准入红/绿基线自带 ScriptedAdjudicate 控制，
不受本默认门影响。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _komari_debug_default_admit(monkeypatch: pytest.MonkeyPatch) -> None:
    """per-test：把 group_admission 顶替为始终 BUSINESS 获准的替身。"""
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
