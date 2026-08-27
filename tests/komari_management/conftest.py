"""TSK-231 komari_management 管理路由测试统一准入替身。

TSK-230 起生产在持久群工作各效果前接入统一群准入（ADR-0012）。komari_management
的既有路由测试（announce / reply_fulfillment / config 等）不关心准入语义，统一注入
``admitted`` 判定替身使真实准入 seam 不阻断既有行为断言；准入语义本身由
``tests/group_admission/*admission.py`` 专项验收。与 ``tests/komari_memory`` 的
约定一致。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _install_management_admission_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """为既有管理路由测试注入恒 admit 的准入替身。"""
    from tests.group_admission.chat_admission_support import (
        ScriptedAdjudicate,
        install_scripted_adjudicate,
    )

    install_scripted_adjudicate(monkeypatch, ScriptedAdjudicate("admitted"))
