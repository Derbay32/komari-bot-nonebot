"""TSK-222/TSK-223 统一群聊准入可执行验收 manifest（测试真源，非测试模块）。

本文件是 ``group_admission`` 核心裁决、运行时与管理控制面的验收登记册，
按 TSK-218 冻结的三层无绕过证明体系承载第一层（测试专用 contract/effect/
management case）：

- ``AdmissionContractCase``：核心模块自身必须成立的契约行（稳定 ID 使用
  ``group_admission.contract.*``，anchor 指向本票实际 pytest node）；
- ``AdmissionEffectCase``：受治理业务效果行。核心裁决模块 **不拥有** 任何
  governed business sink（平台输出、持久写入、LLM/工具调用等效果接缝由
  TSK-224 及后续接入票各自登记），因此本票 ``ADMISSION_EFFECT_CASES``
  保持空 tuple；
- ``AdmissionManagementCase``：TSK-223 阶段 A 登记的管理控制面契约行。
  控制面操作（策略查询/修改、健康查询）按 ADR-0012 属「始终可进入控制面」
  的系统维护分类（``system_control_plane``），**不是** 需要逐群裁决的受治业
  务效果：不进入 ``ADMISSION_EFFECT_CASES``，不与 ``adjudicate`` 的
  BUSINESS 语义混淆；独立的行类型保证未来 governed effects 仍可按原
  ``AdmissionEffectCase`` 形态无歧义表达；
- manifest ID 只存在于本测试真源，不进入生产 ``contracts.py``，也不替
  未来插件发明 effect row。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AdmissionContractCase:
    """一条核心模块契约登记行。

    ``acceptance_anchor`` 是 pytest node ID（``路径::测试函数``），必须可被
    ``--collect-only`` 实际收集。
    """

    contract_id: str
    owner_module: str
    source_symbol: str
    acceptance_anchor: str


@dataclass(frozen=True, slots=True)
class AdmissionEffectCase:
    """一条受治理业务效果登记行。

    ``work_category`` 取瞬时互动 / 持久群工作 / 既成事实收尾 / 技术清理
    四类之一；``attribution_source`` 描述该效果如何恢复关联群归属。
    """

    effect_id: str
    owner_module: str
    source_symbol: str
    sink_kind: str
    intent: str
    attribution_source: str
    work_category: str
    acceptance_anchor: str


@dataclass(frozen=True, slots=True)
class AdmissionManagementCase:
    """一条管理控制面契约登记行（TSK-223 阶段 A）。

    控制面不是受治理业务效果：``work_category`` 固定为
    ``system_control_plane``（ADR-0012 封闭分类：策略查询/更新与健康检查始
    终可进入控制面），``sink_kind`` 描述该行的控制面接缠类别（只读投
    影 / 严格 CAS 写入 / 内存状态投影 / 错误契约 / 安全审计 / 路由面）。
    """

    management_case_id: str
    owner_module: str
    source_symbol: str
    endpoint: str
    required_permission: str
    sink_kind: str
    work_category: str
    acceptance_anchor: str


#: 核心模块四项契约：裁决入口、运行时状态、快照发布、生命周期。
#: 本票不登记控制面 / 观测 / 入口门禁契约，它们由后续票各自登记。
ADMISSION_CONTRACT_CASES: tuple[AdmissionContractCase, ...] = (
    AdmissionContractCase(
        contract_id="group_admission.contract.adjudicate",
        owner_module="komari_bot.plugins.group_admission",
        source_symbol="adjudicate",
        acceptance_anchor=(
            "tests/group_admission/test_adjudication_matrix.py"
            "::test_blacklist_empty_set_admits_every_legal_group"
        ),
    ),
    AdmissionContractCase(
        contract_id="group_admission.contract.runtime_state",
        owner_module="komari_bot.plugins.group_admission",
        source_symbol="get_runtime_state",
        acceptance_anchor=(
            "tests/group_admission/test_runtime_lifecycle.py"
            "::test_top_level_functions_delegate_to_module_singleton"
        ),
    ),
    AdmissionContractCase(
        contract_id="group_admission.contract.snapshot_publish",
        owner_module="komari_bot.plugins.group_admission",
        source_symbol="_AdmissionRuntime",
        acceptance_anchor=(
            "tests/group_admission/test_runtime_lifecycle.py"
            "::test_valid_strict_higher_snapshot_publishes_ready"
        ),
    ),
    AdmissionContractCase(
        contract_id="group_admission.contract.lifecycle",
        owner_module="komari_bot.plugins.group_admission",
        source_symbol="_AdmissionRuntime",
        acceptance_anchor=(
            "tests/group_admission/test_runtime_lifecycle.py"
            "::test_close_clears_snapshot_and_ignores_later_deliveries"
        ),
    ),
)

#: 核心裁决模块不拥有任何受治理业务效果接缝：它只输出裁决与运行时状态，
#: 不执行平台输出、持久写入、LLM/工具调用或平台读取。效果行由 TSK-224+
#: 的效果所有者票（入口门禁、聊天、履约、记忆、提案、总结、管理）登记，
#: 本票不替未来插件发明 effect row。
ADMISSION_EFFECT_CASES: tuple[AdmissionEffectCase, ...] = ()

#: TSK-223 阶段 A 管理控制面契约行：经顶层 ``register_group_admission_api``
#: 装配的三个专属端点 + 错误契约 + 安全审计 + 注册面。全部属系统控制面分
#: 类，不参与群裁决；阶段 B 将在不改变行类型的前提下追加时间/遥测契约行。
ADMISSION_MANAGEMENT_CASES: tuple[AdmissionManagementCase, ...] = (
    AdmissionManagementCase(
        management_case_id="group_admission.management.policy_get",
        owner_module="komari_bot.plugins.group_admission",
        source_symbol="register_group_admission_api",
        endpoint="GET /api/v2/group-admission/policy",
        required_permission="config:read",
        sink_kind="persistent_policy_read",
        work_category="system_control_plane",
        acceptance_anchor=(
            "tests/group_admission/test_management_api.py"
            "::test_policy_get_returns_normalized_policy_with_strong_etag"
        ),
    ),
    AdmissionManagementCase(
        management_case_id="group_admission.management.policy_put_strict_cas",
        owner_module="komari_bot.plugins.group_admission",
        source_symbol="register_group_admission_api",
        endpoint="PUT /api/v2/group-admission/policy",
        required_permission="config:write",
        sink_kind="strict_cas_policy_write",
        work_category="system_control_plane",
        acceptance_anchor=(
            "tests/group_admission/test_management_api.py"
            "::test_put_success_persists_exactly_one_strict_cas"
            "_and_publishes_before_response"
        ),
    ),
    AdmissionManagementCase(
        management_case_id="group_admission.management.status_projection",
        owner_module="komari_bot.plugins.group_admission",
        source_symbol="register_group_admission_api",
        endpoint="GET /api/v2/group-admission/status",
        required_permission="config:read",
        sink_kind="in_memory_state_projection",
        work_category="system_control_plane",
        acceptance_anchor=(
            "tests/group_admission/test_management_api.py"
            "::test_status_ready_projects_singleton_state_with_zero_storage_io"
        ),
    ),
    AdmissionManagementCase(
        management_case_id="group_admission.management.error_whitelist",
        owner_module="komari_bot.plugins.group_admission",
        source_symbol="register_group_admission_api",
        endpoint="PUT /api/v2/group-admission/policy",
        required_permission="config:write",
        sink_kind="closed_error_contract",
        work_category="system_control_plane",
        acceptance_anchor=(
            "tests/group_admission/test_management_api.py"
            "::test_put_persisted_but_unpublished_returns_503"
            "_and_runtime_degrades"
        ),
    ),
    AdmissionManagementCase(
        management_case_id="group_admission.management.audit_safety",
        owner_module="komari_bot.plugins.group_admission",
        source_symbol="register_group_admission_api",
        endpoint="PUT /api/v2/group-admission/policy",
        required_permission="config:write",
        sink_kind="management_audit",
        work_category="system_control_plane",
        acceptance_anchor=(
            "tests/group_admission/test_management_api.py"
            "::test_put_success_audit_metadata_exact_safe_fields"
        ),
    ),
    AdmissionManagementCase(
        management_case_id="group_admission.management.registration_surface",
        owner_module="komari_bot.plugins.group_admission",
        source_symbol="register_group_admission_api",
        endpoint="GET/PUT /api/v2/group-admission/*",
        required_permission="config:read",
        sink_kind="route_registration",
        work_category="system_control_plane",
        acceptance_anchor=(
            "tests/group_admission/test_management_registration.py"
            "::test_registration_is_idempotent_with_exactly_three_routes"
        ),
    ),
)
