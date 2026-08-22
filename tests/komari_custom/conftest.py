"""komari_custom 既有单元测试的统一准入运行时 probe。

TSK-226 起生产 komari_custom 在每个不可分业务效果前消费统一准入裁决
（经 ``group_admission`` 顶层 ``adjudicate`` 内部解析 ``_runtime``）。本
conftest 为既有单元测试默认注入一个「全部群获准」的 probe，保持既有行为不
变；需要验证受限语义的用例自行安装受限 probe（见
``tests/group_admission.custom_acceptance_support.AdmissionProbe``，位于
``tests/group_admission`` 验收套件中）。
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

import pytest

from tests.group_admission.custom_acceptance_support import AdmissionProbe

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _admit_all_groups(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    probe = AdmissionProbe(admitted=True, revision=1)
    runtime_module = importlib.import_module(
        "komari_bot.plugins.group_admission.runtime"
    )
    monkeypatch.setattr(runtime_module, "_runtime", probe)
    yield
