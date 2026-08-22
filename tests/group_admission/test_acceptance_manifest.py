"""TSK-222/TSK-223：acceptance manifest 自身的最小契约测试。

验收目标（manifest 作为测试真源必须自洽）：

- contract/effect/management/observability 稳定 ID 唯一且前缀形态受控；
- 每行全部字段非空；
- 本票恰好登记核心四项契约，anchor 指向本票实际可收集的 pytest node；
- 核心模块不拥有受治理业务效果，effect 登记保持空（控制面 CAS 属
  ``system_control_plane``，可观测性/通知属 ``operational_diagnostic``，都不冒
  充 governed BUSINESS effect）；
- TSK-223 阶段 A 登记六条管理控制面契约行，全部属 ``system_control_plane``
  分类（不把全局控制面 CAS 当成需要群裁决的 BUSINESS 效果），anchor 同
  样可被收集；
- TSK-223 阶段 B 登记八条无内容可观测性契约行，全部属
  ``operational_diagnostic`` 分类，owner/source/anchor 可收集；
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
    ADMISSION_MANAGEMENT_CASES,
    ADMISSION_OBSERVABILITY_CASES,
    AdmissionContractCase,
    AdmissionEffectCase,
    AdmissionManagementCase,
    AdmissionObservabilityCase,
)

pytestmark = pytest.mark.group_admission_acceptance

PROJECT_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_CORE_CONTRACT_IDS = {
    "group_admission.contract.adjudicate",
    "group_admission.contract.runtime_state",
    "group_admission.contract.snapshot_publish",
    "group_admission.contract.lifecycle",
}

EXPECTED_MANAGEMENT_CASE_IDS = {
    "group_admission.management.policy_get",
    "group_admission.management.policy_put_strict_cas",
    "group_admission.management.status_projection",
    "group_admission.management.error_whitelist",
    "group_admission.management.audit_safety",
    "group_admission.management.registration_surface",
}

EXPECTED_OBSERVABILITY_CASE_IDS = {
    "group_admission.observability.status_full_projection",
    "group_admission.observability.telemetry_closed_low_cardinality",
    "group_admission.observability.normal_denial_silence",
    "group_admission.observability.attribution_window",
    "group_admission.observability.fault_episode",
    "group_admission.observability.notification_fallback",
    "group_admission.observability.notification_offline_combine",
    "group_admission.observability.content_safety",
}

#: observability 行的生产源符号只允许运行时内部 seam 与装配入口。
ALLOWED_OBSERVABILITY_SOURCE_SYMBOLS = {
    "_AdmissionRuntime",
    "register_group_admission_api",
}


def _all_case_ids() -> list[str]:
    ids = [case.contract_id for case in ADMISSION_CONTRACT_CASES]
    ids.extend(case.effect_id for case in ADMISSION_EFFECT_CASES)
    ids.extend(case.management_case_id for case in ADMISSION_MANAGEMENT_CASES)
    ids.extend(
        case.observability_case_id for case in ADMISSION_OBSERVABILITY_CASES
    )
    return ids


def test_contract_case_ids_are_unique_and_well_formed() -> None:
    contract_ids = [case.contract_id for case in ADMISSION_CONTRACT_CASES]
    assert len(contract_ids) == len(set(contract_ids)), "contract ID 必须唯一"
    for contract_id in contract_ids:
        assert contract_id.startswith("group_admission.contract."), contract_id

    effect_ids = [case.effect_id for case in ADMISSION_EFFECT_CASES]
    assert len(effect_ids) == len(set(effect_ids)), "effect ID 必须唯一"

    management_ids = [
        case.management_case_id for case in ADMISSION_MANAGEMENT_CASES
    ]
    assert len(management_ids) == len(set(management_ids)), "management ID 必须唯一"
    for management_id in management_ids:
        assert management_id.startswith("group_admission.management."), (
            management_id
        )

    observability_ids = [
        case.observability_case_id for case in ADMISSION_OBSERVABILITY_CASES
    ]
    assert len(observability_ids) == len(set(observability_ids)), (
        "observability ID 必须唯一"
    )
    for observability_id in observability_ids:
        assert observability_id.startswith("group_admission.observability."), (
            observability_id
        )

    # 跨行类型全局唯一：不同分类不得共享稳定 ID。
    assert len(_all_case_ids()) == len(set(_all_case_ids())), "跨行类型 ID 重复"


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
    for case in ADMISSION_MANAGEMENT_CASES:
        for field in dataclasses.fields(case):
            value = getattr(case, field.name)
            assert isinstance(value, str) and value.strip(), (
                f"{case.management_case_id} 字段 {field.name} 为空"
            )
    for case in ADMISSION_OBSERVABILITY_CASES:
        for field in dataclasses.fields(case):
            value = getattr(case, field.name)
            assert isinstance(value, str) and value.strip(), (
                f"{case.observability_case_id} 字段 {field.name} 为空"
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
    登记；本票保持空 tuple，不替未来插件发明 effect row。管理控制面 CAS
    写入是全局系统配置维护（``system_control_plane``），登记在
    ``ADMISSION_MANAGEMENT_CASES``；阶段 B 的计数/窗口/故障期与 SUPERUSER
    通知是封闭运维诊断（``operational_diagnostic``），登记在
    ``ADMISSION_OBSERVABILITY_CASES``；两者都不冒充需要逐群裁决的受治理业务效果。
    """
    assert ADMISSION_EFFECT_CASES == ()


def test_management_cases_register_exactly_the_phase_a_control_plane() -> None:
    """阶段 A 恰好登记六条控制面契约行，全部属系统控制面分类。"""
    assert {case.management_case_id for case in ADMISSION_MANAGEMENT_CASES} == (
        EXPECTED_MANAGEMENT_CASE_IDS
    )
    for case in ADMISSION_MANAGEMENT_CASES:
        assert case.owner_module == "komari_bot.plugins.group_admission"
        assert case.source_symbol == "register_group_admission_api"
        assert case.work_category == "system_control_plane", (
            f"{case.management_case_id} 被错误分类为业务效果: "
            f"{case.work_category}"
        )
        assert case.endpoint.startswith(
            ("GET /api/v2/group-admission", "PUT /api/v2/group-admission", "GET/PUT /api/v2/group-admission")
        ), case.endpoint
    assert dataclasses.is_dataclass(AdmissionManagementCase)
    params = getattr(AdmissionManagementCase, "__dataclass_params__", None)
    assert bool(getattr(params, "frozen", False)) is True


def test_observability_cases_register_exactly_the_phase_b_rows() -> None:
    """阶段 B 恰好登记八条可观测性契约行，全部属运维诊断分类。"""
    assert {
        case.observability_case_id for case in ADMISSION_OBSERVABILITY_CASES
    } == EXPECTED_OBSERVABILITY_CASE_IDS
    for case in ADMISSION_OBSERVABILITY_CASES:
        assert case.owner_module == "komari_bot.plugins.group_admission"
        assert case.source_symbol in ALLOWED_OBSERVABILITY_SOURCE_SYMBOLS, (
            f"{case.observability_case_id} 源符号越界: {case.source_symbol}"
        )
        assert case.work_category == "operational_diagnostic", (
            f"{case.observability_case_id} 被错误分类: {case.work_category}"
        )
        assert case.surface.strip() and case.sink_kind.strip()
    assert dataclasses.is_dataclass(AdmissionObservabilityCase)
    params = getattr(AdmissionObservabilityCase, "__dataclass_params__", None)
    assert bool(getattr(params, "frozen", False)) is True


def test_contract_anchors_are_collectable_pytest_nodes() -> None:
    anchors = [case.acceptance_anchor for case in ADMISSION_CONTRACT_CASES]
    anchors.extend(
        case.acceptance_anchor for case in ADMISSION_MANAGEMENT_CASES
    )
    anchors.extend(
        case.acceptance_anchor for case in ADMISSION_OBSERVABILITY_CASES
    )
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
