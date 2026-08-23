#!/usr/bin/env python3
"""TSK-233 统一群聊准入验收门闭集证据生成器。

从仓库真实状态生成可复现的准入验收证据（TSK-233 总调度裁定）：

- ``manifest``：五桶（contracts / effects / command_effects / management /
  observability）登记行数，来源 ``tests.group_admission.acceptance_manifest``
  （测试真源，直接 import 常量表，不触发任何 NoneBot 插件加载副作用）；
- ``anchors_total``：manifest 全部 ``acceptance_anchor`` 去重计数；
- ``collection``：对 ``group_admission_acceptance`` 与
  ``group_admission_service`` 两个 marker 各真实执行一次
  ``pytest --collect-only -q -m <marker> tests/group_admission/``，解析
  selected / deselected 计数；
- ``digest``：除 ``digest`` 外全字段 canonical JSON
  （``json.dumps(..., sort_keys=True, separators=(",", ":"))``）的 SHA-256 hex。

输出确定可复现：同一 commit 上两次运行输出全等，不掺时间戳与任何业务内容
（正文 / 群号 / 用户号）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.group_admission.acceptance_manifest import (
    ADMISSION_COMMAND_EFFECT_CASES,
    ADMISSION_CONTRACT_CASES,
    ADMISSION_EFFECT_CASES,
    ADMISSION_MANAGEMENT_CASES,
    ADMISSION_OBSERVABILITY_CASES,
)

COLLECTION_MARKERS: tuple[str, ...] = (
    "group_admission_acceptance",
    "group_admission_service",
)

#: ``-q`` 收集摘要形如 ``413/421 tests collected (8 deselected) in 0.86s``。
_COLLECT_SUMMARY_RE = re.compile(
    r"(\d+)/(\d+) tests collected(?: \(([0-9]+) deselected\))?"
)
#: 兼容无 deselected 或非 ``-q`` 形态的摘要兜底。
_COLLECT_SUMMARY_RE_ALT = re.compile(r"(\d+) tests collected(?:, ([0-9]+) deselected)?")


def _commit_short() -> str:
    """取 git HEAD 短哈希（7 位，子进程真实读取）。"""
    proc = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return proc.stdout.strip()


def _manifest() -> dict[str, int]:
    """五桶登记行数，与 ``acceptance_manifest`` 真源一致。"""
    return {
        "contracts": len(ADMISSION_CONTRACT_CASES),
        "effects": len(ADMISSION_EFFECT_CASES),
        "command_effects": len(ADMISSION_COMMAND_EFFECT_CASES),
        "management": len(ADMISSION_MANAGEMENT_CASES),
        "observability": len(ADMISSION_OBSERVABILITY_CASES),
    }


def _anchors() -> set[str]:
    """manifest 五桶全部 ``acceptance_anchor`` 去重集合。"""
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


def _parse_collect_summary(output: str) -> dict[str, int]:
    """从 ``pytest --collect-only -q`` 输出解析 selected / deselected。

    优先匹配 ``-q`` 形态（``selected/total tests collected (N deselected)``），
    失败时回退到 ``N tests collected(, M deselected)`` 形态；计数一律来自
    pytest 真实输出。
    """
    primary = list(_COLLECT_SUMMARY_RE.finditer(output))
    if primary:
        match = primary[-1]
        selected = int(match.group(1))
        if match.group(3) is not None:
            deselected = int(match.group(3))
        else:
            deselected = int(match.group(2)) - selected
        return {"selected": selected, "deselected": deselected}
    alt = list(_COLLECT_SUMMARY_RE_ALT.finditer(output))
    if alt:
        match = alt[-1]
        total = int(match.group(1))
        deselected = int(match.group(2)) if match.group(2) is not None else 0
        return {"selected": total - deselected, "deselected": deselected}
    raise RuntimeError(  # noqa: TRY003
        f"无法从 pytest 输出解析收集摘要: {output!r}"
    )


def _collect_counts(marker: str) -> dict[str, int]:
    """对单个 marker 真实执行 collect-only，返回 selected / deselected。"""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-m",
            marker,
            "tests/group_admission/",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(  # noqa: TRY003
            f"pytest 收集 {marker!r} 失败（exit {proc.returncode}）\n"
            f"stdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return _parse_collect_summary(proc.stdout)


def _digest(payload: dict[str, object]) -> str:
    """canonical JSON 的 SHA-256 hex（64 位小写）。"""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _build_evidence() -> dict[str, object]:
    """组装除 ``digest`` 外的全部证据字段，再补上 digest。"""
    payload: dict[str, object] = {
        "schema_version": "1",
        "commit": _commit_short(),
        "manifest": _manifest(),
        "collection": {
            marker: _collect_counts(marker) for marker in COLLECTION_MARKERS
        },
        "anchors_total": len(_anchors()),
    }
    payload["digest"] = _digest(payload)
    return payload


def _render_text(evidence: dict[str, object]) -> str:
    """人类可读的文本形态（自由格式，与 json 同一份数据）。"""
    lines = [
        f"commit={evidence['commit']}",
        f"anchors_total={evidence['anchors_total']}",
        f"digest={evidence['digest']}",
    ]
    manifest = evidence["manifest"]
    assert isinstance(manifest, dict)
    for bucket, count in manifest.items():
        lines.append(f"manifest.{bucket}={count}")
    collection = evidence["collection"]
    assert isinstance(collection, dict)
    for marker, counts in collection.items():
        assert isinstance(counts, dict)
        lines.append(f"collection.{marker}.selected={counts['selected']}")
        lines.append(f"collection.{marker}.deselected={counts['deselected']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="生成统一群聊准入验收门闭集证据（TSK-233）"
    )
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="输出形态：json（验收契约）或 text（人类可读）",
    )
    args = parser.parse_args(argv)
    evidence = _build_evidence()
    if args.format == "json":
        sys.stdout.write(json.dumps(evidence, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(_render_text(evidence) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
