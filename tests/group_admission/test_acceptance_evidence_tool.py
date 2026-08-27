"""TSK-233：``tools/acceptance_evidence.py`` 证据生成器验收。

设计契约（TSK-233 总调度裁定）：

- 可 ``python tools/acceptance_evidence.py --format json`` 执行，输出单行 / 缩进
  JSON，closed 键集为 {``schema_version``, ``commit``, ``manifest``,
  ``collection``, ``anchors_total``, ``digest``}；
- ``manifest`` 为各桶行数（``contracts`` / ``effects`` / ``command_effects`` /
  ``management`` / ``observability``），与 ``tests.group_admission
  .acceptance_manifest`` 真源一致；
- ``collection`` 为 ``pytest --collect-only -m`` 实际执行的各 marker
  selected / deselected 计数；
- ``anchors_total`` 为 manifest 全部 ``acceptance_anchor`` 去重计数；
- ``digest`` 为除 digest 外全字段 canonical JSON（``sort_keys`` +
  ``separators=(",", ":")``）的 SHA-256 hex；
- 输出绝不含业务内容（正文 / 群号 / 用户号等，canary 哨兵验证）。

红基线：``tools/acceptance_evidence.py`` 尚未实现，本文件按目标缺失失败。
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
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
TOOL_PATH = PROJECT_ROOT / "tools" / "acceptance_evidence.py"

CLOSED_KEYS = {
    "schema_version",
    "commit",
    "manifest",
    "collection",
    "anchors_total",
    "digest",
}
MANIFEST_BUCKET_KEYS = {
    "contracts",
    "effects",
    "command_effects",
    "management",
    "observability",
}
COLLECTION_MARKERS = ("group_admission_acceptance", "group_admission_service")

#: canary：合成正文 / 群号 / 用户号哨兵，绝不允许出现在证据输出中。
CANARY_BODY_TEXT = "TSK233哨兵正文不应出现在发布证据"
CANARY_GROUP_ID = "88886666"
CANARY_USER_ID = "77775555"

@functools.lru_cache(maxsize=1)
def _run_tool() -> subprocess.CompletedProcess[str]:
    """运行证据生成器（进程内缓存，同 commit 输出确定性）。"""
    assert TOOL_PATH.is_file(), f"证据生成器不存在: {TOOL_PATH}"
    return subprocess.run(
        [sys.executable, str(TOOL_PATH), "--format", "json"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def _run_tool_fresh() -> subprocess.CompletedProcess[str]:
    """强制重新运行证据生成器（确定性对比用）。"""
    assert TOOL_PATH.is_file(), f"证据生成器不存在: {TOOL_PATH}"
    return subprocess.run(
        [sys.executable, str(TOOL_PATH), "--format", "json"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def _tool_data() -> dict[str, object]:
    proc = _run_tool()
    assert proc.returncode == 0, (
        f"证据生成器退出码 {proc.returncode}\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    return json.loads(proc.stdout)


def _manifest_counts() -> dict[str, int]:
    return {
        "contracts": len(ADMISSION_CONTRACT_CASES),
        "effects": len(ADMISSION_EFFECT_CASES),
        "command_effects": len(ADMISSION_COMMAND_EFFECT_CASES),
        "management": len(ADMISSION_MANAGEMENT_CASES),
        "observability": len(ADMISSION_OBSERVABILITY_CASES),
    }


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


def test_tool_output_has_exactly_the_closed_key_set() -> None:
    data = _tool_data()
    assert set(data) == CLOSED_KEYS
    assert data["schema_version"] == "1"


def test_manifest_counts_match_the_truth_source() -> None:
    data = _tool_data()
    manifest = data["manifest"]
    assert isinstance(manifest, dict)
    assert set(manifest) == MANIFEST_BUCKET_KEYS
    for bucket, count in _manifest_counts().items():
        assert manifest[bucket] == count, (
            f"manifest.{bucket} 应为 {count}，实际 {manifest[bucket]}"
        )


def test_commit_is_short_git_head() -> None:
    commit = _tool_data()["commit"]
    assert isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{7,40}", commit), commit


def test_collection_reports_both_markers_from_real_pytest_collection() -> None:
    data = _tool_data()
    collection = data["collection"]
    assert isinstance(collection, dict)
    assert set(collection) == set(COLLECTION_MARKERS)
    anchors_total = data["anchors_total"]
    assert isinstance(anchors_total, int) and anchors_total > 0
    for marker in COLLECTION_MARKERS:
        entry = collection[marker]
        assert isinstance(entry, dict)
        assert set(entry) == {"selected", "deselected"}
        assert isinstance(entry["selected"], int) and entry["selected"] >= 0
        assert isinstance(entry["deselected"], int) and entry["deselected"] >= 0
    # acceptance selector 必须至少选中全部 manifest 锚点对应的测试。
    assert collection["group_admission_acceptance"]["selected"] >= anchors_total


def test_anchors_total_is_distinct_anchor_count() -> None:
    assert _tool_data()["anchors_total"] == len(_all_anchors())


def test_digest_is_canonical_json_sha256_and_stable() -> None:
    data = _tool_data()
    digest = data["digest"]
    assert isinstance(digest, str)
    assert re.fullmatch(r"[0-9a-f]{64}", digest), digest

    payload = {key: value for key, value in data.items() if key != "digest"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert digest == expected, "digest 与 canonical JSON 重算不一致"


def test_two_runs_on_same_commit_are_identical() -> None:
    first = json.loads(_run_tool_fresh().stdout)
    second = json.loads(_run_tool_fresh().stdout)
    assert first == second


def test_output_contains_no_business_content_canaries() -> None:
    proc = _run_tool_fresh()
    assert proc.returncode == 0
    for canary in (CANARY_BODY_TEXT, CANARY_GROUP_ID, CANARY_USER_ID):
        assert canary not in proc.stdout, f"证据输出泄露业务内容: {canary}"
