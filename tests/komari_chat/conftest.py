"""komari_chat 既有聊天逻辑测试的准入默认门（TSK-225）。

komari_chat 编排层现已在每个瞬时聊天效果前接入 ``group_admission`` 顶层
``adjudicate`` 裁决（ADR-0012）。既有聊天逻辑测试（test_message_handler /
test_vision_service / test_tsk196 / test_tsk197 等）关注聊天内部逻辑而非准入
验收，默认策略应视为「获准开展业务」。

本 conftest 为该目录全部用例注入一个 per-test 作用域的「默认放行」顶替
``adjudicate``（BUSINESS 获准）+ READY 运行时可读面，恢复接入前的「效果默认
放行」语义，且不遮蔽按目录隔离的准入专项验收。

注意：tests/group_admission 的准入红/绿基线自带 ScriptedAdjudicate 控制，
不受本默认门影响。chat_admission_support 懒 import 避免 NoneBot 装载期副作用，
本 fixture 同样在用例执行期惰性 import。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _komari_chat_default_admit(monkeypatch: pytest.MonkeyPatch) -> None:
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
