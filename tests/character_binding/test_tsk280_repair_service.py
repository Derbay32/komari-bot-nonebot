"""TSK-280 修复服务公共 seam 接口契约（无服务阶段为缺失业务模块 RED）。

生产 ``repair`` 模块尚未实现：本文件顶层 import 失败即基线 RED 证据；
实现落地后这些用例固定公共接口形状，与 ``TSK-280-contract.md`` 一致。
"""

from __future__ import annotations

import inspect
from dataclasses import fields
from typing import TYPE_CHECKING

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


def test_repair_service_constructor_requires_session_factory_and_clock() -> None:
    """构造函数关键字依赖与可选 manager 参数。"""
    parameters = inspect.signature(BindingRepairService.__init__).parameters
    keyword_names = {
        name
        for name, parameter in parameters.items()
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY
    }
    assert {"session_factory", "clock"} <= keyword_names
    assert "manager" in keyword_names
    assert parameters["manager"].default is None


def test_repair_service_rejects_construction_without_dependencies() -> None:
    with pytest.raises(TypeError):
        BindingRepairService()  # type: ignore[call-arg]


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
    ]


def test_repair_scope_literal_has_only_member_and_group() -> None:
    assert set(RepairScope.__args__) == {"member", "group"}
