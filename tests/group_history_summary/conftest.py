"""group_history_summary 服务测试公共 fixture。

TSK-206 起总结执行不再读取 PostgreSQL Prompt 表；本目录只保留群准入默认
门，避免既有内部逻辑测试被逐效果准入误伤。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _group_history_summary_default_admit(monkeypatch: pytest.MonkeyPatch) -> None:
    """TSK-229：既有总结逻辑测试的准入默认门。

    群历史总结编排层现已在每个瞬时效果前接入 ``group_admission`` 顶层
    ``adjudicate`` 裁决（ADR-0012）。本目录的服务/候选/渲染测试关注总结内部
    业务规则而非逐效果准入验收，默认策略应视为「获准开展业务」。本 fixture
    为该目录全部用例注入
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
