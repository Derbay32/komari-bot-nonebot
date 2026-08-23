"""TSK-233：manifest 全部 acceptance_anchor 必须被 acceptance selector 收集。

设计契约（TSK-233 总调度裁定）：``pytest --collect-only -m
group_admission_acceptance tests/group_admission/`` 的收集节点集必须 ⊇ manifest
全部 ``acceptance_anchor``（按 node id 精确匹配）；锚点所在文件被 selector
选中即算覆盖。

红基线：``test_custom_proposal_admission`` / ``test_custom_vote_epoch`` /
``test_forgetting_admission`` / ``test_interaction_v2_admission`` /
``test_memory_dormancy_admission`` 五个文件缺 ``pytestmark``，其锚点目前不被
acceptance selector 收集（缺失清单见失败信息）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.group_admission.acceptance_manifest import (
    ADMISSION_COMMAND_EFFECT_CASES,
    ADMISSION_CONTRACT_CASES,
    ADMISSION_EFFECT_CASES,
    ADMISSION_MANAGEMENT_CASES,
    ADMISSION_OBSERVABILITY_CASES,
)

pytestmark = pytest.mark.group_admission_acceptance

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GROUP_ADMISSION_DIR = PROJECT_ROOT / "tests" / "group_admission"


def _all_anchors() -> set[str]:
    anchors: set[str] = set()
    for rows in (
        ADMISSION_CONTRACT_CASES,
        ADMISSION_EFFECT_CASES,
        ADMISSION_MANAGEMENT_CASES,
        ADMISSION_OBSERVABILITY_CASES,
        ADMISSION_COMMAND_EFFECT_CASES,
    ):
        for case in rows:
            anchors.add(case.acceptance_anchor)
    return anchors


def test_every_manifest_anchor_is_collected_by_acceptance_selector() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "-m",
            "group_admission_acceptance",
            str(GROUP_ADMISSION_DIR),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, (
        f"acceptance selector 收集失败\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    collected = {line.strip() for line in proc.stdout.splitlines()}
    missing = sorted(_all_anchors() - collected)
    assert not missing, f"未被 acceptance selector 收集的锚点: {missing}"
