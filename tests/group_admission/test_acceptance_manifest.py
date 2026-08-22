"""TSK-222/TSK-223：acceptance manifest 自身的最小契约测试。

验收目标（manifest 作为测试真源必须自洽）：

- contract/effect/management/observability 稳定 ID 唯一且前缀形态受控；
- 每行全部字段非空；
- 本票恰好登记核心四项契约，anchor 指向本票实际可收集的 pytest node；
- 核心模块不拥有受治理业务效果，effect 登记保持单条（控制面 CAS 属
  ``system_control_plane``，可观测性/通知属 ``operational_diagnostic``，都不冒
  充 governed BUSINESS effect）；TSK-224 已登记一条入口门禁效果，不替代下游；
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

EXPECTED_EFFECT_CASE_IDS = {
    "group_admission.effect.inbound_matcher_dispatch",
    "group_admission.effect.custom.session_business_clock",
    "group_admission.effect.custom.publishing_claim",
    "group_admission.effect.custom.vote_message_send",
    "group_admission.effect.custom.emoji_like_read",
    "group_admission.effect.custom.knowledge_commit",
    "group_admission.effect.custom.approval_notice",
    "group_admission.effect.custom.publication_reconciliation",
    "group_admission.effect.chat.llm_round",
    "group_admission.effect.chat.tool_dispatch",
    "group_admission.effect.chat.tool_search",
    "group_admission.effect.chat.tool_fetch_page",
    "group_admission.effect.chat.vision_completion",
    "group_admission.effect.chat.image_download",
    "group_admission.effect.chat.embedding",
    "group_admission.effect.chat.group_read",
    "group_admission.effect.chat.reaction",
    "group_admission.effect.chat.reply_send",
    "group_admission.effect.chat.fixed_failure_text",
    "group_admission.effect.chat.debug_public",
    "group_admission.effect.summary.history_read",
    "group_admission.effect.summary.planning_llm",
    "group_admission.effect.summary.summary_llm",
    "group_admission.effect.summary.image_render",
    "group_admission.effect.summary.group_output",
    "group_admission.effect.summary.debug_public",
    "group_admission.effect.fulfillment.prepare",
    "group_admission.effect.fulfillment.send_start",
    "group_admission.effect.fulfillment.recover_send",
    "group_admission.effect.fulfillment.reconcile",
    "group_admission.effect.fulfillment.commitment",
    "group_admission.effect.fulfillment.cleanup",
    "group_admission.effect.memory.conversation_claim",
    "group_admission.effect.memory.conversation_body_read",
    "group_admission.effect.memory.interaction_global_commit",
    "group_admission.effect.memory.forgetting_decay",
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


def test_effect_case_inbound_matcher_dispatch_registered() -> None:
    """ADMISSION_EFFECT_CASES 恰好登记入口、TSK-226 custom 与 TSK-225 chat 行。

    TSK-224 登记入口门禁效果 ``inbound_matcher_dispatch``；TSK-226 追加
    komari_custom 群归属生命周期效果行；TSK-225 追加 komari_chat 可独立
    撤销瞬时效果行。三组来源与意图不同，各自按模块校验。
    """
    assert {case.effect_id for case in ADMISSION_EFFECT_CASES} == (
        EXPECTED_EFFECT_CASE_IDS
    )


def test_effect_case_inbound_row_shape_is_frozen() -> None:
    """入口门禁单行保持固定形状：owner=group_admission/business/transient。"""
    inbound_cases = [
        case
        for case in ADMISSION_EFFECT_CASES
        if case.effect_id == "group_admission.effect.inbound_matcher_dispatch"
    ]
    assert len(inbound_cases) == 1
    case = inbound_cases[0]
    assert case.owner_module == "komari_bot.plugins.group_admission"
    assert case.intent == "business"
    assert case.attribution_source == "onebot_v11_event_group_id"
    assert case.work_category == "transient_interaction"


def test_custom_effect_case_rows_are_attributed() -> None:
    """TSK-226 custom 效果行归属 komari_custom，intent 仅取受控闭集。"""
    custom_prefix = "group_admission.effect.custom."
    custom_cases = [
        case
        for case in ADMISSION_EFFECT_CASES
        if case.effect_id.startswith(custom_prefix)
    ]
    assert len(custom_cases) >= 1, "TSK-226 必须登记 custom 效果行"
    for case in custom_cases:
        assert case.owner_module == "komari_bot.plugins.komari_custom", (
            f"{case.effect_id} 归属模块越界: {case.owner_module}"
        )
        assert case.intent in {"business", "fact_finalization"}, case.effect_id
        assert case.attribution_source.strip() and case.sink_kind.strip()


def test_chat_effect_case_rows_are_attributed() -> None:
    """TSK-225 chat 效果行归属 komari_chat，全部为 business 瞬时互动。"""
    chat_prefix = "group_admission.effect.chat."
    chat_cases = [
        case
        for case in ADMISSION_EFFECT_CASES
        if case.effect_id.startswith(chat_prefix)
    ]
    assert len(chat_cases) >= 1, "TSK-225 必须登记 chat 效果行"
    for case in chat_cases:
        assert case.owner_module == "komari_bot.plugins.komari_chat", (
            f"{case.effect_id} 归属模块越界: {case.owner_module}"
        )
        assert case.intent == "business", case.effect_id
        assert case.work_category == "transient_interaction", case.effect_id
        assert case.attribution_source == "komari_chat_message_group_id"


def test_fulfillment_effect_case_rows_are_attributed() -> None:
    """TSK-228 回复履约冻结承诺效果行归属 komari_chat，intent/work 受控。"""
    full_prefix = "group_admission.effect.fulfillment."
    rows = [
        case
        for case in ADMISSION_EFFECT_CASES
        if case.effect_id.startswith(full_prefix)
    ]
    assert len(rows) >= 2, "TSK-228 必须登记 fulfillment 效果行"
    for case in rows:
        assert case.owner_module == "komari_bot.plugins.komari_chat", case.effect_id
        assert case.intent in {"business", "fact_finalization", "technical_cleanup"}
        assert case.work_category in {
            "transient_interaction",
            "fact_finalization",
            "technical_cleanup",
        }, case.effect_id
        assert case.attribution_source == "reply_fulfillment.group_id", case.effect_id


def test_memory_effect_case_rows_are_attributed() -> None:
    """TSK-230 记忆 dormancy 效果行归属 komari_memory，intent 受控。"""
    memory_prefix = "group_admission.effect.memory."
    memory_cases = [
        case
        for case in ADMISSION_EFFECT_CASES
        if case.effect_id.startswith(memory_prefix)
    ]
    assert len(memory_cases) >= 4, "TSK-230 必须登记 memory 效果行"
    for case in memory_cases:
        assert case.owner_module == "komari_bot.plugins.komari_memory", case.effect_id
        assert case.intent == "business", case.effect_id


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
    anchors.extend(
        case.acceptance_anchor for case in ADMISSION_EFFECT_CASES
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
