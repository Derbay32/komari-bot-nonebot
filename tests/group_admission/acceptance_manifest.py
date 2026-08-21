"""TSK-222 统一群聊准入可执行验收 manifest（测试真源，非测试模块）。

本文件是 ``group_admission`` 核心裁决与运行时的验收登记册，按 TSK-218
冻结的三层无绕过证明体系承载第一层（测试专用 contract/effect case）：

- ``AdmissionContractCase``：核心模块自身必须成立的契约行（稳定 ID 使用
  ``group_admission.contract.*``，anchor 指向本票实际 pytest node）；
- ``AdmissionEffectCase``：受治理业务效果行。核心裁决模块 **不拥有** 任何
  governed business sink（平台输出、持久写入、LLM/工具调用等效果接缝由
  TSK-224 及后续接入票各自登记），因此本票 ``ADMISSION_EFFECT_CASES``
  保持空 tuple；
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
