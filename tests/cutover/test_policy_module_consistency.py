"""TSK-232 轮 B —— 运行时插件与共享包的 canonical 真值交叉一致性（红基线）。

锁定设计契约：``komari_bot.plugins.group_admission.policy``（运行时准入
真值，已存在）与 ``komari_bot.admission_policy``（cutover 共享 canonical
真值，轮 B 新建）对同一组输入必须产出一致的 canonical 形态与一致的准入
裁决——复用方式为 re-export 或薄封装均可，语义一致性由本文件锁定。

- 合法矩阵：两面对同一载荷给出同一 mode/group_ids 集合；
- 准入真值：以共享包 canonical 形态按 ADR-0012 真值表独立推导，与运行时
  ``policy_admits`` 在黑/白名单 x 空/非空 x 关联群组合下逐一相等；
- 拒绝矩阵：一面拒绝的非法输入另一面必须同样拒绝。

红基线失败原因 = 共享模块 ``komari_bot.admission_policy`` 不存在
（ModuleNotFoundError）。本文件无 DB、可正常收集、Ruff/Pyright 零错误。
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

import pytest

from komari_bot.plugins.group_admission.policy import (
    PolicyCompilationError,
    compile_policy,
    policy_admits,
)
from tests.cutover.support import (
    CANARY_GROUP_A,
    CANARY_GROUP_B,
    CANARY_GROUP_C,
)

if TYPE_CHECKING:
    from types import ModuleType


def _admission_policy_module() -> ModuleType:
    """动态解析共享包：缺失时运行期抛 ModuleNotFoundError（红基线）。"""
    return importlib.import_module("komari_bot.admission_policy")


def _canonicalize(payload: object) -> dict[str, Any]:
    """隔离导入点：共享包缺失时让用例以 ModuleNotFoundError 红。"""
    result: dict[str, Any] = _admission_policy_module().canonicalize_policy(payload)
    return result


#: 合法策略矩阵：乱序/重复群号也必须收敛到同一 canonical 形态。
VALID_MATRIX: tuple[tuple[str, list[int]], ...] = (
    ("blacklist", []),
    ("blacklist", [CANARY_GROUP_A]),
    ("blacklist", [CANARY_GROUP_B, CANARY_GROUP_A, CANARY_GROUP_B]),
    ("whitelist", []),
    ("whitelist", [CANARY_GROUP_C]),
    ("whitelist", [CANARY_GROUP_A, CANARY_GROUP_B, CANARY_GROUP_C]),
)

_VALID_MATRIX_IDS = [f"{mode}-{index}" for index, (mode, _) in enumerate(VALID_MATRIX)]

#: 拒绝矩阵：任一面拒绝则两面都必须拒绝。
INVALID_MATRIX: tuple[dict[str, Any], ...] = (
    {"mode": "open", "group_ids": []},
    {"mode": "blacklist", "group_ids": [True]},
    {"mode": "blacklist", "group_ids": [-1]},
    {"mode": "whitelist", "group_ids": ["123"]},
    {"mode": "whitelist", "group_ids": [0]},
    {"mode": "blacklist"},
    {"mode": "blacklist", "group_ids": [], "extra": None},
)

#: 关联群组合：空集、单群、多群、超集。
ASSOCIATED_MATRIX: tuple[frozenset[int], ...] = (
    frozenset(),
    frozenset({CANARY_GROUP_A}),
    frozenset({CANARY_GROUP_A, CANARY_GROUP_B}),
    frozenset({CANARY_GROUP_C, 999}),
)


def _expected_admission(
    mode: str,
    group_ids: set[int],
    associated: frozenset[int],
) -> bool:
    """ADR-0012 真值表 oracle：空名单全放行；黑名单命中受限；白名单子集放行。"""
    if not group_ids:
        return True
    if mode == "blacklist":
        return not group_ids.intersection(associated)
    return associated.issubset(group_ids)


# ---------------------------------------------------------------------------
# 合法矩阵：canonical 形态一致
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("mode", "raw_ids"), VALID_MATRIX, ids=_VALID_MATRIX_IDS)
def test_both_surfaces_agree_on_canonical_form(
    mode: str,
    raw_ids: list[int],
) -> None:
    """共享包 canonical 输出与运行时编译产物表达同一策略。"""
    payload: dict[str, Any] = {"mode": mode, "group_ids": raw_ids}

    compiled = compile_policy(dict(payload))
    canonical = _canonicalize(dict(payload))

    assert canonical == {
        "mode": mode,
        "group_ids": sorted(set(raw_ids)),
    }
    assert canonical["mode"] == compiled.mode
    assert set(canonical["group_ids"]) == set(compiled.group_ids)


# ---------------------------------------------------------------------------
# 准入真值一致
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("mode", "raw_ids"), VALID_MATRIX, ids=_VALID_MATRIX_IDS)
@pytest.mark.parametrize(
    "associated", ASSOCIATED_MATRIX, ids=["empty", "single", "pair", "superset"]
)
def test_admission_truth_matches_between_surfaces(
    mode: str,
    raw_ids: list[int],
    associated: frozenset[int],
) -> None:
    """运行时裁决与共享包 canonical 形态独立推导的真值逐组合相等。"""
    compiled = compile_policy({"mode": mode, "group_ids": raw_ids})
    runtime_truth = policy_admits(compiled, associated)
    shared_payload = _canonicalize({"mode": mode, "group_ids": raw_ids})
    shared_truth = _expected_admission(
        str(shared_payload["mode"]),
        set(shared_payload["group_ids"]),
        associated,
    )
    assert runtime_truth is shared_truth


# ---------------------------------------------------------------------------
# 拒绝矩阵：两面同拒同放
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", INVALID_MATRIX)
def test_invalid_payload_rejected_by_both_surfaces(payload: dict[str, Any]) -> None:
    """非法输入在共享包与运行时编译两侧必须同时被拒。"""
    error_type: type[Exception] = _admission_policy_module().PolicyCanonicalizationError

    with pytest.raises(error_type):
        _canonicalize(dict(payload))
    with pytest.raises(PolicyCompilationError):
        compile_policy(dict(payload))


def test_shared_error_is_value_error_subclass() -> None:
    """PolicyCanonicalizationError 与既有 PolicyCompilationError 同族（ValueError）。"""
    shared_error: type[Exception] = (
        _admission_policy_module().PolicyCanonicalizationError
    )

    for error_type in (shared_error, PolicyCompilationError):
        assert issubclass(error_type, ValueError)
