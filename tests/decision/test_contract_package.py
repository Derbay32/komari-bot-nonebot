"""komari_bot.decision 判定契约共享包验收测试（ticket #28）。

验收目标：
- 共享包顶层统一 re-export 全部判定契约符号；
- 共享包保持零依赖纯件成色（不 import nonebot / redis / 插件内部类型）；
- 契约类型行为与搬迁前逐字节等价（枚举值、dataclass 字段与顺序、类方法、
  关键字-only 构造）。
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import typing
from pathlib import Path

import pytest

from komari_bot import decision as contracts

PACKAGE_DIR = Path(contracts.__file__).resolve().parent

EXPECTED_EXPORTS = {
    "DecisionRuntimeState",
    "DecisionRuntimeStatus",
    "DecisionOutcome",
    "CallIntent",
    "MemoryAction",
    "ReplyReason",
    "UnifiedRerankResult",
    "CandidateSchema",
    "SceneRuntimeUnavailableError",
    "TimingScoreBreakdown",
    "FilterResult",
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


def test_package_reexports_all_contract_symbols() -> None:
    missing = EXPECTED_EXPORTS.difference(dir(contracts))
    assert not missing, f"共享包缺少导出符号: {sorted(missing)}"
    exported = set(contracts.__all__)
    assert EXPECTED_EXPORTS.issubset(exported)


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


def test_runtime_status_is_strenum_with_original_values() -> None:
    status = contracts.DecisionRuntimeStatus
    assert issubclass(status, str)
    assert {member.value for member in status} == {"ready", "disabled", "failed"}
    assert status.READY.value == "ready"
    assert status.DISABLED.value == "disabled"
    assert status.FAILED.value == "failed"


def test_runtime_state_behaviour_matches_legacy() -> None:
    state_cls = contracts.DecisionRuntimeState
    status = contracts.DecisionRuntimeStatus

    assert dataclasses.is_dataclass(state_cls)
    assert state_cls.__dataclass_params__.frozen is True
    assert [field.name for field in dataclasses.fields(state_cls)] == [
        "status",
        "reason",
    ]

    ready = state_cls.ready()
    assert ready.status is status.READY
    assert ready.reason == "scene runtime 已就绪"
    assert ready.is_ready is True

    disabled = state_cls.disabled("已关闭")
    assert disabled.status is status.DISABLED
    assert disabled.reason == "已关闭"
    assert disabled.is_ready is False

    failed = state_cls.failed("初始化失败")
    assert failed.status is status.FAILED
    assert failed.reason == "初始化失败"
    assert failed.is_ready is False

    with pytest.raises(dataclasses.FrozenInstanceError):
        ready.reason = "改写"  # type: ignore[misc]


def test_literal_aliases_match_legacy() -> None:
    assert set(typing.get_args(contracts.CallIntent)) == {
        "none",
        "ambiguous",
        "direct_call",
        "casual_mention",
    }
    assert set(typing.get_args(contracts.MemoryAction)) == {"store", "drop"}
    assert set(typing.get_args(contracts.ReplyReason)) == {
        "at",
        "direct_call",
        "score",
        "none",
    }


def test_decision_outcome_fields_match_legacy() -> None:
    outcome = contracts.DecisionOutcome
    assert dataclasses.is_dataclass(outcome)
    assert outcome.__dataclass_params__.frozen is True
    assert [field.name for field in dataclasses.fields(outcome)] == [
        "memory_action",
        "should_reply",
        "force_reply",
        "reply_reason",
        "forced_reply_reason",
        "reply_score",
        "alias_hit",
        "call_intent",
        "call_margin",
        "best_scene_id",
        "scene_score",
        "timing_score",
        "noise_score",
        "meaningful_score",
        "call_direct_score",
        "call_mention_score",
        "filter_reason",
        "rank_result",
        "timing_breakdown",
        "runtime_status",
        "runtime_reason",
    ]


def test_candidate_schema_fields_match_legacy() -> None:
    schema = contracts.CandidateSchema
    assert dataclasses.is_dataclass(schema)
    assert schema.__dataclass_params__.frozen is True
    fields = {field.name: field for field in dataclasses.fields(schema)}
    assert list(fields) == [
        "key",
        "text",
        "kind",
        "scene_id",
        "embedding_similarity",
    ]
    assert fields["scene_id"].default is None
    assert fields["embedding_similarity"].default is None


def test_unified_rerank_result_fields_match_legacy() -> None:
    result = contracts.UnifiedRerankResult
    assert dataclasses.is_dataclass(result)
    assert result.__dataclass_params__.frozen is True
    assert [field.name for field in dataclasses.fields(result)] == [
        "alias_hit",
        "candidates",
        "score_map",
        "meaningful_score",
        "noise_score",
        "call_direct_score",
        "call_mention_score",
        "best_scene_id",
        "best_scene_score",
        "meaningful_prior",
        "noise_prior",
    ]


def test_scene_runtime_unavailable_error_is_runtime_error() -> None:
    assert issubclass(contracts.SceneRuntimeUnavailableError, RuntimeError)


def test_timing_score_breakdown_fields_match_legacy() -> None:
    breakdown = contracts.TimingScoreBreakdown
    assert dataclasses.is_dataclass(breakdown)
    assert breakdown.__dataclass_params__.frozen is True
    assert [field.name for field in dataclasses.fields(breakdown)] == [
        "timing_score",
        "mention_bonus",
        "silence_bonus",
        "activity_penalty",
        "dialogue_penalty",
        "cooldown_penalty",
        "activity_count",
        "unique_users",
        "silence_gap_seconds",
        "bot_gap_seconds",
    ]


def test_filter_result_is_frozen_and_keyword_only() -> None:
    result_cls = contracts.FilterResult
    assert dataclasses.is_dataclass(result_cls)
    assert result_cls.__dataclass_params__.frozen is True
    assert [field.name for field in dataclasses.fields(result_cls)] == [
        "should_skip",
        "reason",
    ]

    result = result_cls(should_skip=True, reason="short")
    assert result.should_skip is True
    assert result.reason == "short"

    with pytest.raises(TypeError):
        result_cls(True, "short")  # type: ignore[call-arg]

    with pytest.raises(dataclasses.FrozenInstanceError):
        result.should_skip = False  # type: ignore[misc]


def test_decision_engine_protocol_declares_evaluate_signature() -> None:
    protocol = contracts.DecisionEngineProtocol
    assert getattr(protocol, "_is_protocol", False) is True

    evaluate = protocol.evaluate
    signature = inspect.signature(evaluate)
    parameters = signature.parameters
    assert list(parameters) == ["self", "message_content", "group_id", "at_trigger"]
    for name in ("message_content", "group_id", "at_trigger"):
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
