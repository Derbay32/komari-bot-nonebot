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
- ``AdmissionObservabilityCase``：TSK-223 阶段 B 登记的无内容可观测性契约行。
  计数、窗口、故障期与通知属运维诊断分类（``operational_diagnostic``，
  TSK-217）：它们是封闭的运维观测/告警面，不是需要群裁决的受治理业务效果，
  因此同样不进入 ``ADMISSION_EFFECT_CASES``；
- manifest ID 只存在于本测试真源，不进入生产 ``contracts.py``，也不替
  未来插件发明 effect row。
"""

from __future__ import annotations

from dataclasses import dataclass

from tests.group_admission.command_effect_sink import (
    COMMAND_EFFECT_SINK_CENSUS,
)


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


@dataclass(frozen=True, slots=True)
class AdmissionObservabilityCase:
    """一条无内容可观测性契约登记行（TSK-223 阶段 B）。

    可观测性不是受治理业务效果：``work_category`` 固定为
    ``operational_diagnostic``（TSK-217：计数/窗口/故障期/通知属运维诊断，
    不冒充 governed BUSINESS effect）；``surface`` 描述该行的观察面（状态投
    影 / 结构化日志 / SUPERUSER 卡 / 内存遥测），``sink_kind`` 描述契约类
    别。ID 前缀 ``group_admission.observability.``。
    """

    observability_case_id: str
    owner_module: str
    source_symbol: str
    surface: str
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

#: TSK-224 slice4 效果登记：入口门禁效果行。
#:
#: ``group_admission.effect.inbound_matcher_dispatch`` 是事件门控的唯一受治理
#: 业务效果——它经过裁决后把准入群事件投递到 ``nonebot_matcher_dispatch``。
#: 此效果属于 ``transient_interaction`` 工作类别（瞬时互动），不持久化、
#: 不写入存储。
#:
#: 核心裁决模块（``adjudicate`` / ``get_runtime_state``）仍不拥有任何受治理
#: 业务效果接缝：它只输出裁决与运行时状态，不执行平台输出、持久写入、
#: LLM/工具调用或平台读取。管理控制面 CAS 写入属 ``system_control_plane``
#: （登记在 ``ADMISSION_MANAGEMENT_CASES``）；可观测性属
#: ``operational_diagnostic``（登记在 ``ADMISSION_OBSERVABILITY_CASES``）；
#: 两者都不冒充需要逐群裁决的 governed BUSINESS effect。
#:
#: TSK-224 已登记入口门禁效果。下游效果（聊天、履约、记忆、提案、总结等）
#: 由后续票各自登记，本票不替代。
ADMISSION_EFFECT_CASES: tuple[AdmissionEffectCase, ...] = (
    AdmissionEffectCase(
        effect_id="group_admission.effect.inbound_matcher_dispatch",
        owner_module="komari_bot.plugins.group_admission",
        source_symbol="_admission_event_gate",
        sink_kind="nonebot_matcher_dispatch",
        intent="business",
        attribution_source="onebot_v11_event_group_id",
        work_category="transient_interaction",
        acceptance_anchor=(
            "tests/group_admission/test_event_gate_flow.py"
            "::test_event_gate_restricted_message_observes_no_phases"
        ),
    ),
    #: TSK-226 komari_custom 群归属生命周期效果登记。
    #:
    #: 每条受治理业务效果都带独立的 ``effect_id`` 前缀
    #: ``group_admission.effect.custom.*``，intent 覆盖 ``business`` /
    #: ``fact_finalization``，owner_module 均为 ``komari_custom``。AC1 要求
    #: 各阶段、intent、平台读取/发送与 global commit 具有独立 effect ID 与
    #: anchor（锚点指向真实 pytest node）。proposal → 知识库的 global commit
    #: （``add_knowledge`` 成功）在提案 `publishing->voting->approving->
    #: approved` 生命周期之外属于单个不可分效果，携带本群 lineage 裁决。
    AdmissionEffectCase(
        effect_id="group_admission.effect.custom.session_business_clock",
        owner_module="komari_bot.plugins.komari_custom",
        source_symbol="CustomSessionManager",
        sink_kind="redis_edit_session_cas",
        intent="business",
        attribution_source="custom:session:{group_id}:{user_id}",
        work_category="persistent_group_work",
        acceptance_anchor=(
            "tests/group_admission/test_custom_proposal_admission.py"
            "::test_restricted_session_edit_session_state_is_frozen_and_no_pttl_renewal"
        ),
    ),
    AdmissionEffectCase(
        effect_id="group_admission.effect.custom.publishing_claim",
        owner_module="komari_bot.plugins.komari_custom",
        source_symbol="ProposalPublicationService",
        intent="business",
        sink_kind="claim_publication",
        attribution_source="proposal.group_id",
        work_category="factual_group_work",
        acceptance_anchor=(
            "tests/group_admission/test_custom_proposal_admission.py"
            "::test_restricted_claim_does_not_acquire_business_lease"
        ),
    ),
    AdmissionEffectCase(
        effect_id="group_admission.effect.custom.vote_message_send",
        owner_module="komari_bot.plugins.komari_custom",
        source_symbol="ProposalPublicationService/__init__",
        intent="business",
        sink_kind="onebot_send_group_msg",
        attribution_source="proposal.group_id",
        work_category="transient_interaction",
        acceptance_anchor=(
            "tests/group_admission/test_custom_proposal_admission.py"
            "::test_restricted_vote_message_not_dispatched"
        ),
    ),
    AdmissionEffectCase(
        effect_id="group_admission.effect.custom.emoji_like_read",
        owner_module="komari_bot.plugins.komari_custom",
        source_symbol="vote_handler.fetch_and_update_votes",
        intent="business",
        sink_kind="onebot_fetch_emoji_like",
        attribution_source="proposal.group_id",
        work_category="transient_interaction",
        acceptance_anchor=(
            "tests/group_admission/test_custom_proposal_admission.py"
            "::test_restricted_emoji_like_not_read"
        ),
    ),
    AdmissionEffectCase(
        effect_id="group_admission.effect.custom.knowledge_commit",
        owner_module="komari_bot.plugins.komari_custom",
        source_symbol="vote_handler.approve_if_ready",
        intent="business",
        sink_kind="knowledge_add_global_commit",
        attribution_source="proposal.group_id",
        work_category="factual_group_work",
        acceptance_anchor=(
            "tests/group_admission/test_custom_proposal_admission.py"
            "::test_approved_fact_finalization_no_duplicate_commit"
        ),
    ),
    AdmissionEffectCase(
        effect_id="group_admission.effect.custom.approval_notice",
        owner_module="komari_bot.plugins.komari_custom",
        source_symbol="vote_handler.approve_if_ready",
        intent="business",
        sink_kind="onebot_send_group_msg",
        attribution_source="proposal.group_id",
        work_category="transient_interaction",
        acceptance_anchor=(
            "tests/group_admission/test_custom_proposal_admission.py"
            "::test_no_approval_notification_resend"
        ),
    ),
    AdmissionEffectCase(
        effect_id="group_admission.effect.custom.publication_reconciliation",
        owner_module="komari_bot.plugins.komari_custom",
        source_symbol="ProposalPublicationService",
        intent="fact_finalization",
        sink_kind="publication_reconciliation",
        attribution_source="proposal.group_id",
        work_category="fact_finalization",
        acceptance_anchor=(
            "tests/group_admission/test_custom_proposal_admission.py"
            "::test_publication_absence_is_unknown_and_never_auto_resends"
        ),
    ),
    # TSK-225 聊天即时效果登记：komari_chat 及其直接编排的可独立撤销瞬时
    # 效果（LLM 轮 / 工具 dispatch / 搜索 / 抓页 / 视觉 / 图片下载 /
    # Embedding / 平台读取 / 表情 / 群输出 / 固定失败文本 / debug 群公开输
    # 出）。全部属瞬时互动，intent=business，归属来自聊天消息所在群。
    AdmissionEffectCase(
        effect_id="group_admission.effect.chat.llm_round",
        owner_module="komari_bot.plugins.komari_chat",
        source_symbol="_execute_tool_loop/_call_llm_completion",
        sink_kind="llm_provider_round",
        intent="business",
        attribution_source="komari_chat_message_group_id",
        work_category="transient_interaction",
        acceptance_anchor=(
            "tests/group_admission/test_chat_effect_admission.py"
            "::test_llm_round_restricted_blocks_provider_call"
        ),
    ),
)

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

#: TSK-223 阶段 B 无内容可观测性契约行：完整状态投影、低基数遥测、正常拒
#: 绝静默、归属 5 分钟窗口、故障期生命周期、通知 fallback / 离线合并与内容安
#: 全。全部属 ``operational_diagnostic`` 分类（不冒充 governed BUSINESS
#: effect）。
ADMISSION_OBSERVABILITY_CASES: tuple[AdmissionObservabilityCase, ...] = (
    AdmissionObservabilityCase(
        observability_case_id="group_admission.observability.status_full_projection",
        owner_module="komari_bot.plugins.group_admission",
        source_symbol="register_group_admission_api",
        surface="GET /api/v2/group-admission/status",
        sink_kind="in_memory_state_projection",
        work_category="operational_diagnostic",
        acceptance_anchor=(
            "tests/group_admission/test_observability_status.py"
            "::test_status_projects_exact_full_field_set_when_ready"
        ),
    ),
    AdmissionObservabilityCase(
        observability_case_id=(
            "group_admission.observability.telemetry_closed_low_cardinality"
        ),
        owner_module="komari_bot.plugins.group_admission",
        source_symbol="register_group_admission_api",
        surface="GET /api/v2/group-admission/status.telemetry",
        sink_kind="low_cardinality_telemetry",
        work_category="operational_diagnostic",
        acceptance_anchor=(
            "tests/group_admission/test_observability_telemetry.py"
            "::test_telemetry_maps_are_preinitialized_with_closed_keys"
        ),
    ),
    AdmissionObservabilityCase(
        observability_case_id="group_admission.observability.normal_denial_silence",
        owner_module="komari_bot.plugins.group_admission",
        source_symbol="_AdmissionRuntime",
        surface="adjudicate/record_private_input_rejected",
        sink_kind="normal_denial_silence",
        work_category="operational_diagnostic",
        acceptance_anchor=(
            "tests/group_admission/test_observability_telemetry.py"
            "::test_normal_denials_only_count_and_stay_silent"
        ),
    ),
    AdmissionObservabilityCase(
        observability_case_id="group_admission.observability.attribution_window",
        owner_module="komari_bot.plugins.group_admission",
        source_symbol="_AdmissionRuntime",
        surface="structured-log+superuser-card",
        sink_kind="throttled_attribution_window",
        work_category="operational_diagnostic",
        acceptance_anchor=(
            "tests/group_admission/test_observability_attribution.py"
            "::test_first_attribution_failure_logs_and_queues_exactly_one_card"
        ),
    ),
    AdmissionObservabilityCase(
        observability_case_id="group_admission.observability.fault_episode",
        owner_module="komari_bot.plugins.group_admission",
        source_symbol="_AdmissionRuntime",
        surface="structured-log+status",
        sink_kind="fault_episode_lifecycle",
        work_category="operational_diagnostic",
        acceptance_anchor=(
            "tests/group_admission/test_observability_fault_episode.py"
            "::test_cold_start_failure_starts_failed_episode_exactly_once"
        ),
    ),
    AdmissionObservabilityCase(
        observability_case_id="group_admission.observability.notification_fallback",
        owner_module="komari_bot.plugins.group_admission",
        source_symbol="_AdmissionRuntime",
        surface="superuser-card",
        sink_kind="superuser_notification_delivery",
        work_category="operational_diagnostic",
        acceptance_anchor=(
            "tests/group_admission/test_observability_notification.py"
            "::test_per_recipient_bot_fallback_delivers_exactly_once"
        ),
    ),
    AdmissionObservabilityCase(
        observability_case_id=(
            "group_admission.observability.notification_offline_combine"
        ),
        owner_module="komari_bot.plugins.group_admission",
        source_symbol="_AdmissionRuntime",
        surface="superuser-card",
        sink_kind="offline_combined_notification",
        work_category="operational_diagnostic",
        acceptance_anchor=(
            "tests/group_admission/test_observability_notification.py"
            "::test_offline_fault_then_recovery_sends_single_combined_card"
        ),
    ),
    AdmissionObservabilityCase(
        observability_case_id="group_admission.observability.content_safety",
        owner_module="komari_bot.plugins.group_admission",
        source_symbol="_AdmissionRuntime",
        surface="all-diagnostic-surfaces",
        sink_kind="content_safety_canary",
        work_category="operational_diagnostic",
        acceptance_anchor=(
            "tests/group_admission/test_observability_content_safety.py"
            "::test_extended_canary_bundle_self_check_and_report_shape"
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class AdmissionCommandEffectCase:
    """一条受治理命令业务效果登记行（TSK-227）。

    与 ``ADMISSION_EFFECT_CASES`` 中核心模块自己拥有的入口门禁行不同，
    本行由各业务插件命令 handler 的效果 owner 持有：``owner_module`` 取
    对应插件顶层包，``effect_id`` 前缀 ``group_admission.effect.command.``。
    它不是控制面 / 观测行，不冒充需要逐群裁决的 governed BUSINESS effect。
    """

    effect_id: str
    owner_module: str
    matcher_entry_id: str
    sink_kind: str
    intent: str
    work_category: str
    acceptance_anchor: str


#: TSK-227 命令业务效果登记：由 ``command_effect_sink`` census 派生，保证
#: manifest 与 sink census 单一真源一致；不手工复制字段。
ADMISSION_COMMAND_EFFECT_CASES: tuple[AdmissionCommandEffectCase, ...] = tuple(
    AdmissionCommandEffectCase(
        effect_id=row.effect_id,
        owner_module=row.owner_module,
        matcher_entry_id=row.entry_id,
        sink_kind=row.sink_kind,
        intent="business",
        work_category=row.work_category,
        acceptance_anchor=row.acceptance_anchor,
    )
    for row in COMMAND_EFFECT_SINK_CENSUS
)


# TSK-225：在闭合枚举既有效果行之外追加的 komari_chat 效果行（census 双向
# 一致见 test_chat_effect_manifest；行为级/契约级 anchor 见对应测试文件）。
ADMISSION_EFFECT_CASES += (
    AdmissionEffectCase(
        effect_id="group_admission.effect.chat.tool_dispatch",
        owner_module="komari_bot.plugins.komari_chat",
        source_symbol="_execute_business_tool",
        sink_kind="tool_body_execution",
        intent="business",
        attribution_source="komari_chat_message_group_id",
        work_category="transient_interaction",
        acceptance_anchor=(
            "tests/group_admission/test_chat_effect_admission.py"
            "::test_tool_dispatch_restricted_blocks_tool_body"
        ),
    ),
    AdmissionEffectCase(
        effect_id="group_admission.effect.chat.tool_search",
        owner_module="komari_bot.plugins.komari_chat",
        source_symbol="_build_search_tool_result",
        sink_kind="komari_search_http",
        intent="business",
        attribution_source="komari_chat_message_group_id",
        work_category="transient_interaction",
        acceptance_anchor=(
            "tests/group_admission/test_chat_effect_admission.py"
            "::test_tool_search_restricted_blocks_search_web"
        ),
    ),
)


ADMISSION_EFFECT_CASES += (
    AdmissionEffectCase(
        effect_id="group_admission.effect.chat.group_read",
        owner_module="komari_bot.plugins.komari_chat",
        source_symbol="MessageHandler._refetch_reply",
        sink_kind="onebot_get_msg",
        intent="business",
        attribution_source="komari_chat_message_group_id",
        work_category="transient_interaction",
        acceptance_anchor=(
            "tests/group_admission/test_chat_effect_admission.py"
            "::test_group_read_restricted_blocks_get_msg"
        ),
    ),
    AdmissionEffectCase(
        effect_id="group_admission.effect.chat.reaction",
        owner_module="komari_bot.plugins.komari_chat",
        source_symbol="MessageHandler._schedule_reply_reaction",
        sink_kind="onebot_set_msg_emoji_like",
        intent="business",
        attribution_source="komari_chat_message_group_id",
        work_category="transient_interaction",
        acceptance_anchor=(
            "tests/group_admission/test_chat_effect_admission.py"
            "::test_reaction_restricted_blocks_fire_and_forget"
        ),
    ),
)


ADMISSION_EFFECT_CASES += (
    AdmissionEffectCase(
        effect_id="group_admission.effect.chat.fixed_failure_text",
        owner_module="komari_bot.plugins.komari_chat",
        source_symbol="MessageHandler.report_reply_failure",
        sink_kind="onebot_group_error_text",
        intent="business",
        attribution_source="komari_chat_message_group_id",
        work_category="transient_interaction",
        acceptance_anchor=(
            "tests/group_admission/test_chat_effect_admission.py"
            "::test_fixed_failure_text_restricted_not_notified"
        ),
    ),
)


ADMISSION_EFFECT_CASES += (
    AdmissionEffectCase(
        effect_id="group_admission.effect.chat.embedding",
        owner_module="komari_bot.plugins.komari_chat",
        source_symbol="embedding_provider",
        sink_kind="embedding_https",
        intent="business",
        attribution_source="komari_chat_message_group_id",
        work_category="transient_interaction",
        acceptance_anchor=(
            "tests/group_admission/test_chat_seam_admission.py"
            "::test_embedding_restricted_blocks_query_embedding"
        ),
    ),
    AdmissionEffectCase(
        effect_id="group_admission.effect.chat.tool_fetch_page",
        owner_module="komari_bot.plugins.komari_chat",
        source_symbol="_build_fetch_tool_result",
        sink_kind="komari_search_http",
        intent="business",
        attribution_source="komari_chat_message_group_id",
        work_category="transient_interaction",
        acceptance_anchor=(
            "tests/group_admission/test_chat_seam_admission.py"
            "::test_fetch_page_restricted_blocks_fetch"
        ),
    ),
)


ADMISSION_EFFECT_CASES += (
    AdmissionEffectCase(
        effect_id="group_admission.effect.chat.vision_completion",
        owner_module="komari_bot.plugins.komari_chat",
        source_symbol="vision_service.read_image",
        sink_kind="vision_llm_round",
        intent="business",
        attribution_source="komari_chat_message_group_id",
        work_category="transient_interaction",
        acceptance_anchor=(
            "tests/group_admission/test_chat_seam_admission.py"
            "::test_vision_completion_restricted_blocks_vision_llm"
        ),
    ),
    AdmissionEffectCase(
        effect_id="group_admission.effect.chat.image_download",
        owner_module="komari_bot.plugins.komari_chat",
        source_symbol="image_downloader.download_images",
        sink_kind="image_http_download",
        intent="business",
        attribution_source="komari_chat_message_group_id",
        work_category="transient_interaction",
        acceptance_anchor=(
            "tests/group_admission/test_chat_seam_admission.py"
            "::test_image_download_restricted_blocks_download"
        ),
    ),
    AdmissionEffectCase(
        effect_id="group_admission.effect.chat.reply_send",
        owner_module="komari_bot.plugins.komari_chat",
        source_symbol="OneBotReplySender",
        sink_kind="onebot_send_group_msg",
        intent="business",
        attribution_source="komari_chat_message_group_id",
        work_category="transient_interaction",
        acceptance_anchor=(
            "tests/group_admission/test_chat_seam_admission.py"
            "::test_reply_send_restricted_blocks_outbound"
        ),
    ),
    AdmissionEffectCase(
        effect_id="group_admission.effect.chat.debug_public",
        owner_module="komari_bot.plugins.komari_chat",
        source_symbol="generate_debug_reply",
        sink_kind="onebot_debug_public_output",
        intent="business",
        attribution_source="komari_chat_message_group_id",
        work_category="transient_interaction",
        acceptance_anchor=(
            "tests/group_admission/test_chat_seam_admission.py"
            "::test_debug_public_restricted_blocks_group_public_output"
        ),
    ),
)


# TSK-229 群历史总结 (group_history_summary) 与调试总结的逐效果准入登记。
#
# 总结是瞬时互动 (transient_interaction, 见 ADR-0012「瞬时互动被拒绝后立即
# 终止，恢复准入后不复活」)：群历史平台读取、planning LLM、summary LLM、
# 图片渲染与平台输出各自拥有独立 effect ID 前缀 group_admission.effect.summary.，
# 归属来自总结命令所在群 group_history_summary_group_id，intent 均为 business。
# ``summary.group_output`` 约束普通总结在群内逐条输出（文本/图片）的平台发送；
# ``summary.debug_public`` 约束 ``.debug summary --public`` 群公开脱敏结果。
ADMISSION_EFFECT_CASES += (
    AdmissionEffectCase(
        effect_id="group_admission.effect.summary.history_read",
        owner_module="komari_bot.plugins.group_history_summary",
        source_symbol="history_service.fetch_group_history_messages",
        sink_kind="onebot_get_group_msg_history",
        intent="business",
        attribution_source="group_history_summary_group_id",
        work_category="transient_interaction",
        acceptance_anchor=(
            "tests/group_admission/test_summary_admission.py"
            "::test_history_read_restricted_blocks_platform_read"
        ),
    ),
    AdmissionEffectCase(
        effect_id="group_admission.effect.summary.planning_llm",
        owner_module="komari_bot.plugins.group_history_summary",
        source_symbol="planner_service.plan_summary_request",
        sink_kind="llm_provider_round",
        intent="business",
        attribution_source="group_history_summary_group_id",
        work_category="transient_interaction",
        acceptance_anchor=(
            "tests/group_admission/test_summary_admission.py"
            "::test_planning_llm_restricted_blocks_provider"
        ),
    ),
    AdmissionEffectCase(
        effect_id="group_admission.effect.summary.summary_llm",
        owner_module="komari_bot.plugins.group_history_summary",
        source_symbol="summarize_service.summarize_history_messages",
        sink_kind="llm_provider_round",
        intent="business",
        attribution_source="group_history_summary_group_id",
        work_category="transient_interaction",
        acceptance_anchor=(
            "tests/group_admission/test_summary_admission.py"
            "::test_summary_llm_restricted_blocks_provider"
        ),
    ),
    AdmissionEffectCase(
        effect_id="group_admission.effect.summary.image_render",
        owner_module="komari_bot.plugins.group_history_summary",
        source_symbol="image_renderer.render_summary_image_pages_base64",
        sink_kind="image_render_pages",
        intent="business",
        attribution_source="group_history_summary_group_id",
        work_category="transient_interaction",
        acceptance_anchor=(
            "tests/group_admission/test_summary_admission.py"
            "::test_image_render_restricted_blocks_render"
        ),
    ),
    AdmissionEffectCase(
        effect_id="group_admission.effect.summary.group_output",
        owner_module="komari_bot.plugins.group_history_summary",
        source_symbol="handle_group_history_summary",
        sink_kind="onebot_send_group_msg",
        intent="business",
        attribution_source="group_history_summary_group_id",
        work_category="transient_interaction",
        acceptance_anchor=(
            "tests/group_admission/test_summary_admission.py"
            "::test_handler_group_output_restricted_blocks_per_send"
        ),
    ),
    AdmissionEffectCase(
        effect_id="group_admission.effect.summary.debug_public",
        owner_module="komari_bot.plugins.komari_debug",
        source_symbol="reporting.build_and_send_diagnostic_report",
        sink_kind="onebot_debug_public_output",
        intent="business",
        attribution_source="debug_summary_target_group_id",
        work_category="transient_interaction",
        acceptance_anchor=(
            "tests/group_admission/test_summary_admission.py"
            "::test_debug_public_restricted_blocks_group_keeps_private"
        ),
    ),
)
