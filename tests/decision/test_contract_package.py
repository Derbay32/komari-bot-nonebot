"""``komari_bot.decision`` 共享包验收：只剩当前 Jev 运行时契约。

验收目标：
- 共享包只导出三个当前公开符号：协议、分发载体与分发不可用错误；
- 共享包保持零依赖纯件成色（不 import nonebot / redis / 插件内部类型）；
- 类型化协议仍只声明 Jev 的 ``evaluate`` / ``claim_handoff`` /
  ``observe_delivered``；
- ``.jev`` 数据契约仍是不可变纯件，且不再携带旧场景分数/DTO 字段。
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from pathlib import Path

import pytest

from komari_bot import decision as contracts

PACKAGE_DIR = Path(contracts.__file__).resolve().parent

EXPECTED_EXPORTS = {
    "DecisionDispatch",
    "DecisionDispatchUnavailableError",
    "DecisionEngineProtocol",
}

FORBIDDEN_IMPORT_ROOTS = {
    "nonebot",
    "nonebug",
    "redis",
    "aioredis",
    "asyncpg",
    "sqlalchemy",
    "fastapi",
}

LEGACY_RESULT_FIELDS = {
    "alias_hit",
    "best_scene_id",
    "call_intent",
    "call_margin",
    "call_direct_score",
    "call_mention_score",
    "filter_reason",
    "forced_reply_reason",
    "meaningful_score",
    "memory_action",
    "noise_score",
    "rank_result",
    "reply_reason",
    "reply_score",
    "runtime_reason",
    "runtime_status",
    "scene_score",
    "should_reply",
    "timing_breakdown",
    "timing_score",
}


def _collect_import_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                continue
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


def _collect_import_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                continue
            if node.module:
                modules.add(node.module)
    return modules


def _is_frozen_dataclass(cls: type) -> bool:
    """读取 dataclass 的 frozen 配置（pyright 未实现该运行时属性，走 getattr）。"""
    params = getattr(cls, "__dataclass_params__", None)
    return bool(getattr(params, "frozen", False))


def test_package_reexports_exactly_the_supported_contracts() -> None:
    missing = EXPECTED_EXPORTS.difference(dir(contracts))
    assert not missing, f"共享包缺少导出符号: {sorted(missing)}"
    assert set(contracts.__all__) == EXPECTED_EXPORTS


def test_package_reexports_are_the_current_runtime_identities() -> None:
    from komari_bot.decision.dispatch import (
        DecisionDispatch,
        DecisionDispatchUnavailableError,
    )
    from komari_bot.decision.engine_protocol import DecisionEngineProtocol

    assert contracts.DecisionDispatch is DecisionDispatch
    assert (
        contracts.DecisionDispatchUnavailableError is DecisionDispatchUnavailableError
    )
    assert contracts.DecisionEngineProtocol is DecisionEngineProtocol


def test_package_modules_are_dependency_free() -> None:
    module_files = sorted(PACKAGE_DIR.rglob("*.py"))
    assert module_files, "共享包内没有任何模块文件"
    for module_file in module_files:
        roots = _collect_import_roots(module_file)
        bad_roots = roots & FORBIDDEN_IMPORT_ROOTS
        assert not bad_roots, f"{module_file.name} 引入了禁用依赖: {sorted(bad_roots)}"
        modules = _collect_import_modules(module_file)
        plugin_imports = {
            module for module in modules if module.startswith("komari_bot.plugins")
        }
        assert not plugin_imports, (
            f"{module_file.name} 引用了插件内部类型: {sorted(plugin_imports)}"
        )


def test_decision_engine_protocol_declares_typed_jev_interface() -> None:
    """协议只声明消费方需要的三个既有类型化方法（无旧结果适配层）。"""
    from komari_bot.decision.jev import (
        DecisionMessage,
        DecisionRequest,
        DecisionResult,
        HandoffRoute,
    )

    protocol = contracts.DecisionEngineProtocol
    assert getattr(protocol, "_is_protocol", False) is True

    evaluate = inspect.signature(protocol.evaluate).parameters
    assert list(evaluate) == ["self", "request"]
    assert evaluate["request"].annotation == "DecisionRequest"
    assert inspect.signature(protocol.evaluate).return_annotation == "DecisionResult"

    claim = inspect.signature(protocol.claim_handoff).parameters
    assert list(claim) == ["self", "request", "result", "route"]
    assert inspect.signature(protocol.claim_handoff).return_annotation == "bool"

    observe = inspect.signature(protocol.observe_delivered).parameters
    assert list(observe) == [
        "self",
        "message",
        "protocol",
        "account_id",
        "received_at",
    ]
    for name in ("protocol", "account_id", "received_at"):
        assert observe[name].kind is inspect.Parameter.KEYWORD_ONLY
    assert inspect.signature(protocol.observe_delivered).return_annotation == "None"

    assert DecisionRequest.__module__ == "komari_bot.decision.jev"
    assert DecisionResult.__module__ == "komari_bot.decision.jev"
    assert DecisionMessage.__module__ == "komari_bot.decision.jev"
    assert "message_content" not in evaluate
    assert HandoffRoute is not None


def test_jev_result_is_immutable_and_carries_no_legacy_score_fields() -> None:
    """判定结果只保留当前标量事实；旧分数/运行时字段不得回归。"""
    from komari_bot.decision.jev import DecisionResult

    assert dataclasses.is_dataclass(DecisionResult)
    assert _is_frozen_dataclass(DecisionResult) is True
    field_names = {field.name for field in dataclasses.fields(DecisionResult)}
    assert field_names.isdisjoint(LEGACY_RESULT_FIELDS)
    assert {
        "decision_id",
        "status",
        "reason_code",
        "route",
        "effective_reply",
        "summary_probability",
        "optional_selected",
    }.issubset(field_names)

    result = DecisionResult(
        decision_id="decision-immutable",
        status="success",
        reason_code="classified",
    )
    assert result.route == "none"
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.route = "chat"  # type: ignore[misc]
