"""TSK-233：``tests/group_admission`` 全量测试文件的 marker 闭集验收。

设计契约（TSK-233 总调度裁定）：

- ``tests/group_admission/test_*.py`` 全部测试文件必须声明模块级
  ``pytestmark``，取值闭集为 {``group_admission_acceptance``,
  ``group_admission_service``}，零未标记；
- 分类与文件名语义一致：``*_gated``（``*_real_service_gated`` /
  ``*_redis_gated``）文件标记 ``group_admission_service``，其余全部标记
  ``group_admission_acceptance``；
- service 标记文件必须引用真实门控 env（``KOMARI_TEST_POSTGRES_URL`` 或
  ``KOMARI_TEST_REDIS_URL``），保证无服务环境下被 ``skipif`` 门控而非误跑。

本文件自身也是闭集一员（acceptance）；新增 / 改名测试文件都必须遵守闭集，
否则本验收失败。红基线：``test_custom_proposal_admission`` /
``test_custom_vote_epoch`` / ``test_forgetting_admission`` /
``test_interaction_v2_admission`` / ``test_memory_dormancy_admission``
五个文件缺 ``pytestmark``。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.group_admission_acceptance

GROUP_ADMISSION_DIR = Path(__file__).resolve().parent

VALID_MARKERS = frozenset({"group_admission_acceptance", "group_admission_service"})
GATED_ENV_VARS = ("KOMARI_TEST_POSTGRES_URL", "KOMARI_TEST_REDIS_URL")
_GATED_FILE_SUFFIX = "_gated"


def _test_files() -> list[Path]:
    return sorted(GROUP_ADMISSION_DIR.glob("test_*.py"))


def _expected_marker(filename: str) -> str:
    if filename.endswith(f"{_GATED_FILE_SUFFIX}.py"):
        return "group_admission_service"
    return "group_admission_acceptance"


def _pytestmark_marker_names(tree: ast.Module) -> list[str]:
    """从模块级 ``pytestmark`` 赋值提取 ``pytest.mark.<name>`` 标识符。

    支持单值、list、tuple 形态；只识别 ``pytest.mark.<name>`` 属性引用，
    忽略 ``pytest.mark.skipif(...)`` 之类的调用形态。
    """
    names: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in node.targets
        ):
            continue
        names.extend(
            sub.attr
            for sub in ast.walk(node.value)
            if (
                isinstance(sub, ast.Attribute)
                and isinstance(sub.value, ast.Attribute)
                and sub.value.attr == "mark"
            )
        )
    return names


def test_every_group_admission_test_file_has_pytestmark() -> None:
    missing = [
        path.name
        for path in _test_files()
        if not _pytestmark_marker_names(ast.parse(path.read_text("utf-8")))
    ]
    assert not missing, f"缺少 pytestmark 的测试文件: {missing}"


def test_marker_values_follow_filename_semantics() -> None:
    violations: list[str] = []
    for path in _test_files():
        names = set(_pytestmark_marker_names(ast.parse(path.read_text("utf-8"))))
        expected = _expected_marker(path.name)
        if not names:
            violations.append(f"{path.name}: 无 pytestmark（应为 {expected}）")
        elif not names.issubset(VALID_MARKERS):
            violations.append(
                f"{path.name}: 非法 marker {sorted(names - VALID_MARKERS)}"
            )
        elif names != {expected}:
            violations.append(
                f"{path.name}: 标记 {sorted(names)} 与文件名语义不符（应为 {expected}）"
            )
    assert not violations, "\n".join(violations)


def test_service_marked_files_reference_gated_env() -> None:
    violations: list[str] = []
    for path in _test_files():
        if _expected_marker(path.name) != "group_admission_service":
            continue
        text = path.read_text("utf-8")
        if not any(var in text for var in GATED_ENV_VARS):
            violations.append(
                f"{path.name}: service 文件未引用门控 env {GATED_ENV_VARS}"
            )
    assert not violations, "\n".join(violations)
