"""TSK-280 修复服务公共 seam 接口契约（无服务阶段为缺失业务模块 RED）。

生产 ``repair`` 模块尚未实现：本文件顶层 import 失败即基线 RED 证据；
实现落地后这些用例固定公共接口形状，与 ``TSK-280-contract.md`` 一致。
"""

from __future__ import annotations

import inspect
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

from komari_bot.plugins.character_binding.repair import (
    BindingDiagnosis,
    BindingRepairService,
    MemberBindingView,
    RepairBlockedByGameError,
    RepairConfirmResult,
    RepairDependencyChangedError,
    RepairPreview,
    RepairScope,
    RepairTargetNotFoundError,
    RepairTokenError,
    get_binding_repair_service,
    set_binding_repair_service,
)


def test_repair_module_exports_public_seam() -> None:
    """修复模块公开面：服务、DTO、异常与全局存取器。"""
    from komari_bot.plugins.character_binding import repair

    expected = {
        "BindingDiagnosis",
        "BindingRepairService",
        "MemberBindingView",
        "RepairBlockedByGameError",
        "RepairConfirmResult",
        "RepairDependencyChangedError",
        "RepairPreview",
        "RepairScope",
        "RepairTargetNotFoundError",
        "RepairTokenError",
        "get_binding_repair_service",
        "set_binding_repair_service",
    }
    assert expected <= set(repair.__all__)
    assert BindingRepairService is repair.BindingRepairService
    assert set_binding_repair_service is not None
    assert get_binding_repair_service is not None
    assert issubclass(RepairTokenError, RuntimeError)
    assert issubclass(RepairDependencyChangedError, RuntimeError)
    assert issubclass(RepairBlockedByGameError, RuntimeError)
    assert issubclass(RepairTargetNotFoundError, RuntimeError)


def _keyword_only_names(method: Callable[..., object]) -> set[str]:
    return {
        name
        for name, parameter in inspect.signature(method).parameters.items()
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY
    }


def test_repair_service_methods_are_keyword_only_and_take_agreed_parameters() -> None:
    """diagnose/preview/confirm 接受约定关键字参数；member_openid 可缺省。"""
    diagnose = _keyword_only_names(BindingRepairService.diagnose)
    assert {"app_id", "group_openid"} <= diagnose

    preview = _keyword_only_names(BindingRepairService.preview)
    assert {"app_id", "group_openid", "operator_id", "reason"} <= preview
    preview_parameters = inspect.signature(BindingRepairService.preview).parameters
    assert preview_parameters["member_openid"].default is None
    assert preview_parameters["app_id"].kind is inspect.Parameter.KEYWORD_ONLY

    confirm = _keyword_only_names(BindingRepairService.confirm)
    assert {
        "app_id",
        "group_openid",
        "token",
        "operator_id",
        "request_id",
        "reason",
    } <= confirm


def test_repair_service_constructor_requires_session_factory_clock_and_game_state_reader() -> None:
    """构造函数关键字依赖：session_factory/clock/game_state_reader 必填，manager 可选。

    ``game_state_reader`` 从管理 composition root 注入真实 TSK-276
    ``PostgresRouletteStorage.load_current`` 公共 seam；修复核心模块不得
    直接反向 import 轮盘。
    """
    parameters = inspect.signature(BindingRepairService.__init__).parameters
    keyword_names = {
        name
        for name, parameter in parameters.items()
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY
    }
    assert {"session_factory", "clock", "game_state_reader"} <= keyword_names
    assert "manager" in keyword_names
    assert parameters["manager"].default is None


def test_repair_service_rejects_construction_without_dependencies() -> None:
    with pytest.raises(TypeError):
        BindingRepairService()  # type: ignore[call-arg]


def test_repair_service_rejects_construction_without_game_state_reader() -> None:
    """TSK-280：game_state_reader 是必填依赖，缺省构造必须 TypeError。"""
    with pytest.raises(TypeError):
        BindingRepairService(  # type: ignore[call-arg]
            session_factory=lambda: None,  # type: ignore[arg-type]
            clock=lambda: datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
        )


def test_repair_service_has_async_close_lifecycle() -> None:
    """修复服务必须提供异步 close()：关闭后旧引用拒绝操作、令牌失效。"""
    assert inspect.iscoroutinefunction(BindingRepairService.close)


def test_repair_service_has_no_game_command_execution_entry() -> None:
    """修复服务绝不能拥有执行游戏命令的入口（execute_group_command 是严禁
    生产的越界 API；并发创建必须由真实 RouletteCommandService 承担）。"""
    assert not hasattr(BindingRepairService, "execute_group_command")


def test_repair_value_objects_have_fixed_fields() -> None:
    """DTO 字段集合固定，作为 API 响应与审计的公共契约。"""
    assert [field.name for field in fields(MemberBindingView)] == [
        "app_id",
        "group_openid",
        "member_openid",
        "member_qq",
        "character_name",
    ]
    assert [field.name for field in fields(BindingDiagnosis)] == [
        "app_id",
        "group_openid",
        "group_id",
        "members",
        "game_present",
        "game_lifecycle",
    ]
    assert [field.name for field in fields(RepairPreview)] == [
        "token",
        "scope",
        "app_id",
        "group_openid",
        "member_openid",
        "affected_count",
        "cleared_names",
        "version",
        "expires_at",
    ]
    assert [field.name for field in fields(RepairConfirmResult)] == [
        "scope",
        "app_id",
        "group_openid",
        "member_openid",
        "cleared_count",
        "cleared_names",
        "version",
        "expected_count",
    ]


def test_repair_service_exposes_synchronous_confirm_audit_probe() -> None:
    """最终缺陷探针 ``get_confirm_audit_context`` 必须同步、同名、关键字闭集。

    路由在业务调用前用它解析真实目标与预览 version/预期数量，使 ``started``
    事件即携带成员哈希；同步契约可让误 ``await`` 立即 ``TypeError``，而不是
    被整包依赖注入或静默降级掩盖。
    """
    probe: Any = getattr(BindingRepairService, "get_confirm_audit_context", None)
    assert probe is not None, "缺少 BindingRepairService.get_confirm_audit_context"
    assert not inspect.iscoroutinefunction(probe)
    assert _keyword_only_names(probe) == {
        "app_id",
        "group_openid",
        "token",
        "operator_id",
    }


def test_repair_confirm_audit_context_is_frozen_dataclass_with_fixed_fields() -> None:
    """探针值对象 ``RepairConfirmAuditContext``：冻结且字段集合固定。"""
    from komari_bot.plugins.character_binding import repair

    context_type: Any = getattr(repair, "RepairConfirmAuditContext", None)
    assert context_type is not None, "缺少 RepairConfirmAuditContext"
    assert is_dataclass(context_type)
    # ``is_dataclass`` 会把 ``context_type`` 收窄为不含运行期 dunder 的
    # ``DataclassInstance``；用显式 ``Any`` 引用直接读取 ``__dataclass_params__``，
    # 既避免 getattr 字面量（B009）又不触发 pyright 属性未知告警。
    params = cast("Any", context_type).__dataclass_params__
    assert params.frozen is True
    assert [field.name for field in fields(context_type)] == [
        "scope",
        "member_openid",
        "version",
        "expected_count",
    ]


def test_repair_scope_literal_has_only_member_and_group() -> None:
    assert set(RepairScope.__args__) == {"member", "group"}
