"""TSK-222：acceptance manifest 自身的最小契约测试。

验收目标（manifest 作为测试真源必须自洽）：

- contract/effect 稳定 ID 唯一且前缀形态受控；
- 每行全部字段非空；
- 本票恰好登记核心四项契约，anchor 指向本票实际可收集的 pytest node；
- 核心模块不拥有受治理业务效果，effect 登记保持空；
- 不在生产代码登记 manifest ID（生产边界由依赖边界测试单独守护）。
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from pathlib import Path

import pytest

from tests.group_admission.acceptance_manifest import (
    ADMISSION_CONTRACT_CASES,
    ADMISSION_EFFECT_CASES,
    AdmissionContractCase,
    AdmissionEffectCase,
)

pytestmark = pytest.mark.group_admission_acceptance

PROJECT_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_CORE_CONTRACT_IDS = {
    "group_admission.contract.adjudicate",
    "group_admission.contract.runtime_state",
    "group_admission.contract.snapshot_publish",
    "group_admission.contract.lifecycle",
}


def test_contract_case_ids_are_unique_and_well_formed() -> None:
    contract_ids = [case.contract_id for case in ADMISSION_CONTRACT_CASES]
    assert len(contract_ids) == len(set(contract_ids)), "contract ID 必须唯一"
    for contract_id in contract_ids:
        assert contract_id.startswith("group_admission.contract."), contract_id

    effect_ids = [case.effect_id for case in ADMISSION_EFFECT_CASES]
    assert len(effect_ids) == len(set(effect_ids)), "effect ID 必须唯一"


def test_contract_cases_register_exactly_the_four_core_contracts() -> None:
    assert {case.contract_id for case in ADMISSION_CONTRACT_CASES} == (
        EXPECTED_CORE_CONTRACT_IDS
    )
    for case in ADMISSION_CONTRACT_CASES:
        assert case.owner_module == "komari_bot.plugins.group_admission"


def test_every_manifest_row_has_non_empty_fields() -> None:
    for case in ADMISSION_CONTRACT_CASES:
        for field in dataclasses.fields(case):
            value = getattr(case, field.name)
            assert isinstance(value, str) and value.strip(), (
                f"{case.contract_id} 字段 {field.name} 为空"
            )
    for case in ADMISSION_EFFECT_CASES:
        for field in dataclasses.fields(case):
            value = getattr(case, field.name)
            assert isinstance(value, str) and value.strip(), (
                f"{case.effect_id} 字段 {field.name} 为空"
            )


def test_effect_case_shape_covers_required_attribution_fields() -> None:
    field_names = {field.name for field in dataclasses.fields(AdmissionEffectCase)}
    required = {
        "effect_id",
        "owner_module",
        "source_symbol",
        "sink_kind",
        "intent",
        "attribution_source",
        "work_category",
        "acceptance_anchor",
    }
    assert required <= field_names, f"effect case 缺少字段: {required - field_names}"
    assert dataclasses.is_dataclass(AdmissionContractCase)
    params = getattr(AdmissionContractCase, "__dataclass_params__", None)
    assert bool(getattr(params, "frozen", False)) is True


def test_effect_cases_are_empty_because_core_module_owns_no_governed_sink() -> None:
    """核心裁决模块只输出裁决与状态，不拥有受治理业务效果接缝。

    平台输出、持久写入、LLM/工具调用等效果行由 TSK-224 及后续效果所有者票
    登记；本票保持空 tuple，不替未来插件发明 effect row。
    """
    assert ADMISSION_EFFECT_CASES == ()


def test_contract_anchors_are_collectable_pytest_nodes() -> None:
    anchors = [case.acceptance_anchor for case in ADMISSION_CONTRACT_CASES]
    for anchor in anchors:
        assert anchor.startswith("tests/group_admission/"), anchor

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            *anchors,
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    collected = {line.strip() for line in proc.stdout.splitlines()}
    missing = [anchor for anchor in anchors if anchor not in collected]
    assert proc.returncode == 0 and not missing, (
        f"anchor 收集失败: missing={missing}\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
