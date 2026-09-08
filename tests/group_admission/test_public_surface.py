"""TSK-222/TSK-223：group_admission 顶层公开面契约测试。

验收目标：

- 顶层 ``__all__`` 精确等于既有准入符号与 TSK-274 QQ 合同符号，不暴露名单、
  快照、Service、bool
  helper、``create_router``、``force_reload``、effect wrapper、
  Fake/Protocol 或测试 hook；
- 三个枚举的 wire values 与成员集合冻结；
- ``AdmissionResult`` / ``AdmissionRuntimeState`` 为 frozen/slots/keyword-only
  契约类型，字段精确，不提前固定 TSK-223 才拥有的时间/遥测/API 字段；
- ``is_ready`` 仅 READY 为 true，DEGRADED（按 LKG 裁决）不是 ready；
- ``adjudicate`` 同步、无位置 ``intent``、默认 ``BUSINESS``；
  ``get_runtime_state`` 同步、无参数；
- 签名注解运行时真实：``typing.get_type_hints`` 在运行时成功解析两个可调用面，
  结果即公开契约类型（``Collection[int]`` / ``AdmissionIntent`` /
  ``AdmissionResult`` / ``AdmissionRuntimeState``），不因 TYPE_CHECKING-only
  import 抛 NameError；
- reason code / problem code 闭集是真实类型契约：``typing.get_type_hints``
  解析出的 ``Literal`` args 精确等于冻结闭集（含本票 ``adjudicate`` 不会
  产生的 ``private_input_rejected``），不靠扫描字符串字面量证明。

生产包尚不存在时全部用例因缺失 ``komari_bot.plugins.group_admission``
而失败（red），不使用 hasattr/skip/xfail 回避。
"""

from __future__ import annotations

import collections.abc
import dataclasses
import enum
import inspect
import types
import typing
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from types import ModuleType

pytestmark = pytest.mark.group_admission_acceptance

EXPECTED_TOP_LEVEL_SYMBOLS = {
    "adjudicate",
    "get_runtime_state",
    "register_group_admission_api",
    "AdmissionIntent",
    "AdmissionQualification",
    "AdmissionResult",
    "AdmissionRuntimeState",
    "AdmissionRuntimeStatus",
    "QQ_ADMISSION_STATE_KEY",
    "QQAdmissionToken",
    "QQBindClaim",
    "QQInitialBindRequest",
    "QQVerifiedBindingSession",
    "QQEffectDecision",
    "qualify_qq_event",
    "get_qq_admission_token",
    "register_qq_group_resolver",
    "register_qq_initial_bind_claimer",
    "register_qq_binding_session_resolver",
    "register_qq_ban_checker",
    "recheck_qq_effect",
}

FROZEN_REASON_CODES = {
    "policy_admitted",
    "policy_restricted",
    "group_attribution_unavailable",
    "effective_policy_unavailable",
    "fact_finalization_granted",
    "technical_cleanup_granted",
    # 本票 adjudicate 不会产生该码，但闭集必须静态可证明包含它
    # （私聊输入拒绝由后续入口门禁票消费）。
    "private_input_rejected",
}

FROZEN_PROBLEM_CODES = {
    "storage_unavailable",
    "stored_policy_invalid",
    "snapshot_publish_failed",
    "internal_error",
}

FORBIDDEN_PUBLIC_HELPERS = {
    "is_group_allowed",
    "is_admitted",
    "force_reload",
    "get_policy",
    "get_snapshot",
    # TSK-223：装配层只得到注册函数；router/manager/config/service/遥测/
    # 通知器与测试 hook 均不得进入顶层暴露面。
    "create_router",
    "create_group_admission_router",
    "get_manager",
    "get_config",
    "get_service",
    "telemetry",
    "notifier",
    "install_for_testing",
    "reset_for_testing",
}


def _import_package() -> Any:
    import komari_bot.plugins.group_admission as admission

    return admission


def _assert_enum_contract(cls: object, expected: dict[str, str]) -> None:
    """断言 str 枚举成员名→wire value 全等（收窄留在辅助函数内）。"""
    assert isinstance(cls, type) and issubclass(cls, enum.Enum), (
        f"{cls!r} 不是枚举类型"
    )
    assert issubclass(cls, str), f"{cls!r} 必须是 str 枚举（wire value 直接消费）"
    assert {member.name: member.value for member in cls} == expected


def _assert_frozen_slots_dataclass(cls: object, expected_fields: list[str]) -> None:
    """断言 frozen/slots/字段精确的 dataclass 契约（收窄留在辅助函数内）。"""
    assert dataclasses.is_dataclass(cls), f"{cls!r} 不是 dataclass 契约类型"
    params = getattr(cls, "__dataclass_params__", None)
    assert bool(getattr(params, "frozen", False)) is True, "契约类型必须 frozen"
    assert getattr(cls, "__slots__", None), "契约类型必须启用 slots"
    names = [field.name for field in dataclasses.fields(cls)]
    assert names == expected_fields, f"契约字段不精确: {names}"


def test_top_level_all_is_exactly_the_group_and_qq_contract() -> None:
    admission = _import_package()
    assert set(admission.__all__) == EXPECTED_TOP_LEVEL_SYMBOLS
    assert sorted(admission.__all__) == sorted(EXPECTED_TOP_LEVEL_SYMBOLS)

    # 禁止名单采用模块感知判定：包内子模块（``policy`` / ``runtime`` 等）
    # 以属性形式出现在 dir() 中属正常，不算暴露面泄漏。
    namespace = set(dir(admission))
    leaked = {
        name
        for name in FORBIDDEN_PUBLIC_HELPERS & namespace
        if not inspect.ismodule(getattr(admission, name, None))
    }
    assert not leaked, f"顶层暴露面出现禁止的公开符号: {sorted(leaked)}"


def test_admission_intent_wire_values_are_frozen() -> None:
    admission = _import_package()
    _assert_enum_contract(
        admission.AdmissionIntent,
        {
            "BUSINESS": "business",
            "FACT_FINALIZATION": "fact_finalization",
            "TECHNICAL_CLEANUP": "technical_cleanup",
        },
    )


def test_admission_qualification_wire_values_are_frozen() -> None:
    admission = _import_package()
    _assert_enum_contract(
        admission.AdmissionQualification,
        {
            "BUSINESS": "business",
            "FACT_FINALIZATION": "fact_finalization",
            "TECHNICAL_CLEANUP": "technical_cleanup",
            "REJECTED": "rejected",
        },
    )


def test_admission_runtime_status_wire_values_are_frozen() -> None:
    admission = _import_package()
    _assert_enum_contract(
        admission.AdmissionRuntimeStatus,
        {"READY": "ready", "DEGRADED": "degraded", "FAILED": "failed"},
    )


def test_admission_result_is_frozen_slots_keyword_only() -> None:
    admission = _import_package()
    result_cls = admission.AdmissionResult

    _assert_frozen_slots_dataclass(
        result_cls,
        ["qualification", "effective_revision", "reason_code"],
    )

    result = result_cls(
        qualification=admission.AdmissionQualification.REJECTED,
        effective_revision=None,
        reason_code="effective_policy_unavailable",
    )
    assert result.qualification is admission.AdmissionQualification.REJECTED
    assert result.effective_revision is None
    assert result.reason_code == "effective_policy_unavailable"

    with pytest.raises(TypeError):
        result_cls(  # 故意位置调用验证关键字-only
            admission.AdmissionQualification.REJECTED,
            None,
            "effective_policy_unavailable",
        )
    with pytest.raises(AttributeError):
        result.reason_code = "policy_admitted"  # type: ignore[misc]


def test_admission_runtime_state_is_frozen_slots_keyword_only() -> None:
    admission = _import_package()
    state_cls = admission.AdmissionRuntimeState
    status_cls = admission.AdmissionRuntimeStatus

    _assert_frozen_slots_dataclass(
        state_cls,
        [
            "status",
            "problem_code",
            "configured_revision",
            "effective_revision",
            "using_last_known_good",
        ],
    )

    ready = state_cls(
        status=status_cls.READY,
        problem_code=None,
        configured_revision=1,
        effective_revision=1,
        using_last_known_good=False,
    )
    assert ready.status is status_cls.READY
    assert ready.problem_code is None

    with pytest.raises(TypeError):
        state_cls(status_cls.READY, None, 1, 1, False)  # type: ignore[call-arg]  # noqa: FBT003
    with pytest.raises(AttributeError):
        ready.problem_code = "internal_error"  # type: ignore[misc]


def test_is_ready_is_true_only_for_ready_status() -> None:
    admission = _import_package()
    state_cls = admission.AdmissionRuntimeState
    status_cls = admission.AdmissionRuntimeStatus

    ready = state_cls(
        status=status_cls.READY,
        problem_code=None,
        configured_revision=3,
        effective_revision=3,
        using_last_known_good=False,
    )
    degraded = state_cls(
        status=status_cls.DEGRADED,
        problem_code="stored_policy_invalid",
        configured_revision=4,
        effective_revision=3,
        using_last_known_good=True,
    )
    failed = state_cls(
        status=status_cls.FAILED,
        problem_code="storage_unavailable",
        configured_revision=None,
        effective_revision=None,
        using_last_known_good=False,
    )

    assert ready.is_ready is True
    # DEGRADED 虽按 LKG 继续裁决，但不是 ready
    assert degraded.is_ready is False
    assert failed.is_ready is False


def test_adjudicate_signature_is_sync_with_keyword_only_intent_default() -> None:
    admission = _import_package()
    adjudicate = admission.adjudicate

    assert not inspect.iscoroutinefunction(adjudicate), "adjudicate 必须同步"
    parameters = inspect.signature(adjudicate).parameters
    assert list(parameters) == ["associated_group_ids", "intent"]
    assert parameters["associated_group_ids"].kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.POSITIONAL_ONLY,
    )
    intent = parameters["intent"]
    assert intent.kind is inspect.Parameter.KEYWORD_ONLY
    assert intent.default is admission.AdmissionIntent.BUSINESS


def test_get_runtime_state_is_a_sync_no_argument_callable() -> None:
    admission = _import_package()
    get_runtime_state = admission.get_runtime_state

    assert not inspect.iscoroutinefunction(get_runtime_state), (
        "get_runtime_state 必须同步"
    )
    parameters = inspect.signature(get_runtime_state).parameters
    assert list(parameters) == [], "get_runtime_state 不接受任何参数"


def test_register_group_admission_api_is_a_sync_assembly_entrypoint() -> None:
    """注册函数签名冻结：仅供装配层，manager/runtime 不进入 public 签名。

    ``(app, *, api_token, allowed_origins, audit_recorder=None)``；同步、
    返回 ``None``；``audit_recorder`` 可空且默认 ``None``。
    """
    admission = _import_package()
    register = admission.register_group_admission_api

    assert not inspect.iscoroutinefunction(register), "注册函数必须同步"
    parameters = inspect.signature(register).parameters
    assert list(parameters) == [
        "app",
        "api_token",
        "allowed_origins",
        "audit_recorder",
    ]
    assert parameters["app"].kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.POSITIONAL_ONLY,
    )
    for name in ("api_token", "allowed_origins", "audit_recorder"):
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY, name
    assert parameters["allowed_origins"].default is inspect.Parameter.empty
    assert parameters["audit_recorder"].default is None


def test_register_group_admission_api_type_hints_resolve_at_runtime() -> None:
    """注册函数签名注解运行时真实：``get_type_hints`` 不得抛 NameError。

    共享管理包类型（``ManagementTokenSource`` /
    ``ManagementAuditRecorder``）与 FastAPI 类型必须在模块 globals 可解析；
    返回注解为 ``None``。
    """
    from fastapi import FastAPI

    admission = _import_package()

    hints = typing.get_type_hints(admission.register_group_admission_api)

    assert hints["app"] is FastAPI
    assert hints["return"] is types.NoneType
    # 只断言可解析性与基本形态：api_token 允许静态序列或惰性 callable 源；
    # audit_recorder 可为空（可选）。
    assert "api_token" in hints
    assert "allowed_origins" in hints
    audit_hint = hints["audit_recorder"]
    audit_members = (
        typing.get_args(audit_hint)
        if typing.get_origin(audit_hint) in (typing.Union, types.UnionType)
        else (audit_hint,)
    )
    assert types.NoneType in audit_members, (
        f"audit_recorder 必须可空（默认 None）: {audit_hint!r}"
    )


def test_adjudicate_type_hints_resolve_at_runtime() -> None:
    """公开注解真实性：``adjudicate`` 签名注解必须在运行时全部可解析。

    ``typing.get_type_hints`` 不得因 TYPE_CHECKING-only import 抛 NameError；
    只断言解析结果是公开契约类型（``Collection[int]`` / ``AdmissionIntent`` /
    ``AdmissionResult``），不固定内部实现。
    """
    admission = _import_package()

    hints = typing.get_type_hints(admission.adjudicate)

    group_ids_hint = hints["associated_group_ids"]
    assert typing.get_origin(group_ids_hint) is collections.abc.Collection, (
        f"associated_group_ids 必须解析为 Collection[int]，实际为 {group_ids_hint!r}"
    )
    assert typing.get_args(group_ids_hint) == (int,), (
        f"associated_group_ids 元素类型必须为 int，实际为 {group_ids_hint!r}"
    )
    assert hints["intent"] is admission.AdmissionIntent
    assert hints["return"] is admission.AdmissionResult


def test_get_runtime_state_type_hints_resolve_at_runtime() -> None:
    """``get_runtime_state`` 返回注解运行时真实：解析为 ``AdmissionRuntimeState``。"""
    admission = _import_package()

    hints = typing.get_type_hints(admission.get_runtime_state)

    assert hints["return"] is admission.AdmissionRuntimeState


def _frozen_literal_values(hint: object, *, field: str) -> set[str]:
    """把字段类型注解展开为 Literal 字符串值集合。

    只接受 ``Literal[...]`` 或 ``Literal[...] | None``（NoneType 成员被去
    掉）；其他形态直接失败。返回集合同时约束“不多不少”：与冻结闭集做
    精确相等比较。
    """
    origin = typing.get_origin(hint)
    if origin is typing.Literal:
        values: tuple[object, ...] = typing.get_args(hint)
    elif origin in (typing.Union, types.UnionType):
        non_none = [
            arg for arg in typing.get_args(hint) if arg is not types.NoneType
        ]
        assert len(non_none) == 1, (
            f"{field} 只允许是 Literal[...] | None，实际为 {hint!r}"
        )
        literal = non_none[0]
        assert typing.get_origin(literal) is typing.Literal, (
            f"{field} 的非 None 成员必须是 Literal，实际为 {literal!r}"
        )
        values = typing.get_args(literal)
    else:
        msg = f"{field} 注解不是 Literal 也不是可选并集: {hint!r}"
        raise AssertionError(msg)

    assert values, f"{field} 的 Literal 闭集为空: {hint!r}"
    string_values = [value for value in values if isinstance(value, str)]
    assert len(string_values) == len(values), (
        f"{field} 的 Literal 成员必须全部是字符串: {values!r}"
    )
    return set(string_values)


def test_reason_code_literal_contract_is_exactly_the_frozen_closed_set() -> None:
    """闭集是真实类型契约，不是源码字符串扫描。

    ``AdmissionResult.reason_code`` 的 ``typing.get_type_hints`` Literal args
    必须精确等于 7 个冻结 reason codes（实现可用内部 TypeAlias，不要求它进
    顶层 ``__all__``）。本票 ``adjudicate`` 不会产生
    ``private_input_rejected``，但闭集本身必须包含它；运行时实际观察到的码
    由裁决矩阵用例逐一精确断言。
    """
    admission = _import_package()
    hints = typing.get_type_hints(admission.AdmissionResult)
    values = _frozen_literal_values(
        hints["reason_code"], field="AdmissionResult.reason_code"
    )
    assert values == FROZEN_REASON_CODES, (
        "reason_code 闭集与冻结集不一致: "
        f"多出={sorted(values - FROZEN_REASON_CODES)}, "
        f"缺失={sorted(FROZEN_REASON_CODES - values)}"
    )


def test_problem_code_literal_contract_is_exactly_the_frozen_closed_set() -> None:
    """``AdmissionRuntimeState.problem_code`` 去掉 NoneType 后精确等于 4 个
    冻结 problem codes。"""
    admission = _import_package()
    hints = typing.get_type_hints(admission.AdmissionRuntimeState)
    values = _frozen_literal_values(
        hints["problem_code"], field="AdmissionRuntimeState.problem_code"
    )
    assert values == FROZEN_PROBLEM_CODES, (
        "problem_code 闭集与冻结集不一致: "
        f"多出={sorted(values - FROZEN_PROBLEM_CODES)}, "
        f"缺失={sorted(FROZEN_PROBLEM_CODES - values)}"
    )


def test_module_singleton_seam_exists_for_monkeypatching() -> None:
    """顶层函数必须经 ``runtime._runtime`` 属性解析 module singleton。

    测试以真实 ``_AdmissionRuntime`` 实例 monkeypatch 该属性；生产不得新增
    ``_install_for_testing`` / reset hook / Fake / Protocol。
    """
    runtime_module: ModuleType = __import__(
        "komari_bot.plugins.group_admission.runtime",
        fromlist=["_AdmissionRuntime", "_runtime"],
    )
    assert hasattr(runtime_module, "_AdmissionRuntime")
    assert hasattr(runtime_module, "_runtime")
    assert not hasattr(runtime_module, "_install_for_testing")
    assert not hasattr(runtime_module, "reset_for_testing")
