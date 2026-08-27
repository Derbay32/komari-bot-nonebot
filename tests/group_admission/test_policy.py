"""TSK-247 —— 统一策略 canonical/指纹契约验收基线（纯 API + 架构合同，无服务）。

锁定 ``komari_bot.admission_policy`` 公共 pure API 与跨边界契约：

- AC2：单群号、空名单、多群号、重复与顺序变化经 ``policy_fingerprint``
  产生确定、顺序无关的同一指纹（64-hex）；
- AC7（字节级）：指纹必须与「真实 canonical 存储形态」逐字节一致——
  ``policy_fingerprint`` 的序列化输入 == ``canonicalize_policy`` 输出
  （升序去重）的紧凑 JSON 字节，即 0013 迁移对存储 JSONB 文本重算
  digest 的同一字节面；修复前红基线：旧实现多群号下 CLI 与迁移 digest
  分叉（红），当前测试用于防止该缺陷回归；
- AC6（架构/AST 合同）：运行时 ``compile_policy`` 与 CLI canonical 校验
  不得维护两套独立验证真源——运行时 policy 模块必须 import
  ``komari_bot.admission_policy.canonicalize_policy`` 并在 ``compile_policy``
  内引用同一真源（只断言依赖边界，不锁实现行号）；
- AC7（自包含/AST 合同）：0013 迁移必须自包含，不得 import 任何
  ``komari_bot`` 可变应用模块（只断言依赖边界）。

本文件无 DB、无 NoneBot 运行时依赖，是可无服务打红、准确证明多群号分叉
的测试面之一；当前测试用于防止该缺陷回归。
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from komari_bot.admission_policy import canonicalize_policy, policy_fingerprint
from tests.cutover.support import (
    CANARY_GROUP_A,
    CANARY_GROUP_B,
    CANARY_GROUP_C,
    MULTIGROUP_CANONICAL_POLICY,
    MULTIGROUP_RAW_POLICY,
)

pytestmark = pytest.mark.group_admission_acceptance

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _canonical_storage_digest(canonical: dict[str, Any]) -> str:
    """0013 迁移对存储 JSONB 文本重算 digest 的字节面 oracle。

    与迁移内联 ``_canonical_policy_digest`` 的公式同构（sort_keys + 紧凑
    分隔符，直接作用于 canonical 存储形态）；测试据此锁定 CLI 共享指纹与
    迁移 digest 的字节级一致性，不调用迁移私有 helper。
    """
    serialized = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _module_source(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# AC2：指纹确定性与顺序/重复无关（同一策略唯一指纹）
# ---------------------------------------------------------------------------


def test_fingerprint_is_deterministic_64_hex_for_every_shape() -> None:
    """单群号/空名单/多群号各形态指纹确定、64-hex 可解析。"""
    shapes: list[dict[str, Any]] = [
        {"mode": "blacklist", "group_ids": []},
        {"mode": "blacklist", "group_ids": [CANARY_GROUP_A]},
        {"mode": "whitelist", "group_ids": [CANARY_GROUP_A, CANARY_GROUP_B]},
        {"mode": "whitelist", "group_ids": [CANARY_GROUP_B, CANARY_GROUP_A]},
        MULTIGROUP_RAW_POLICY,
    ]
    first = [policy_fingerprint(payload) for payload in shapes]
    second = [policy_fingerprint(payload) for payload in shapes]
    assert first == second, "同输入两次调用必须产出完全一致指纹"
    for fingerprint in first:
        assert len(fingerprint) == 64
        int(fingerprint, 16)  # hex 可解析


def test_fingerprint_is_order_and_duplicate_independent() -> None:
    """顺序变化与重复不改变指纹（同一策略唯一指纹）。"""
    ordered = policy_fingerprint(
        {"mode": "whitelist", "group_ids": [CANARY_GROUP_A, CANARY_GROUP_B]}
    )
    shuffled = policy_fingerprint(
        {"mode": "whitelist", "group_ids": [CANARY_GROUP_B, CANARY_GROUP_A]}
    )
    duplicated = policy_fingerprint(
        {
            "mode": "whitelist",
            "group_ids": [CANARY_GROUP_B, CANARY_GROUP_A, CANARY_GROUP_A],
        }
    )
    assert ordered == shuffled == duplicated


def test_fingerprint_distinguishes_distinct_policies() -> None:
    """不同内容（mode 或群号集合）指纹必然不同。"""
    base = policy_fingerprint(MULTIGROUP_RAW_POLICY)
    other_mode = policy_fingerprint(
        {"mode": "whitelist", "group_ids": MULTIGROUP_CANONICAL_POLICY["group_ids"]}
    )
    other_group = policy_fingerprint(
        {"mode": "blacklist", "group_ids": [CANARY_GROUP_C]}
    )
    assert base != other_mode
    assert base != other_group


# ---------------------------------------------------------------------------
# AC7：指纹与真实 canonical 存储形态逐字节一致（修复前红基线：旧实现多群号分叉 → 红；
# 当前测试用于防止该缺陷回归）
# ---------------------------------------------------------------------------


def test_fingerprint_byte_matches_canonical_storage_form_for_multigroup() -> None:
    """多群号乱序/重复输入的指纹必须等于 canonical 存储字节的 SHA-256。

    0013 迁移对存储 JSONB 文本（canonical 形态）重算 digest；CLI 共享指纹
    必须与它逐字节一致。修复前红基线：旧实现 ``policy_fingerprint`` 对去重
    降序形态取摘要，与存储升序形态分叉（AC3/AC7 违约）；当前测试用于防止
    该缺陷回归。
    """
    canonical = canonicalize_policy(MULTIGROUP_RAW_POLICY)
    assert canonical == MULTIGROUP_CANONICAL_POLICY
    expected = _canonical_storage_digest(canonical)
    # 指纹对 raw 输入与 canonical 输入都必须等于存储字节 digest。
    assert policy_fingerprint(MULTIGROUP_RAW_POLICY) == expected
    assert policy_fingerprint(canonical) == expected


def test_fingerprint_byte_matches_canonical_storage_form_for_single_and_empty() -> None:
    """单群号与空名单同样满足字节级一致性（当前已一致，锁定不回归）。"""
    for mode in ("blacklist", "whitelist"):
        for group_ids in ([], [CANARY_GROUP_A]):
            canonical = canonicalize_policy({"mode": mode, "group_ids": group_ids})
            expected = _canonical_storage_digest(canonical)
            assert policy_fingerprint(canonical) == expected


# ---------------------------------------------------------------------------
# AC6：运行时编译与 CLI canonical 校验共享同一验证真源（架构/AST 合同）
# ---------------------------------------------------------------------------


def test_runtime_compile_reuses_shared_canonical_truth() -> None:
    """运行时 policy 模块必须复用 ``komari_bot.admission_policy`` 真源。

    修复前红基线：旧实现 ``compile_policy`` 自带一套内联校验（不 import 共
    享包），形成两套独立验证真源（AC6 违约）；当前测试用于防止该缺陷回归。
    本测试只断言依赖边界：模块 import 共享包，且 ``compile_policy`` 函数体
    引用 ``canonicalize_policy``，不锁具体实现行号。
    """
    source = _module_source("komari_bot/plugins/group_admission/policy.py")
    tree = ast.parse(source)

    shared_imported = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == (
            "komari_bot.admission_policy"
        ):
            shared_imported = True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "komari_bot.admission_policy":
                    shared_imported = True
    assert shared_imported, (
        "runtime policy 模块必须 import komari_bot.admission_policy（单一验证真源）"
    )

    compile_fns = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "compile_policy"
    ]
    assert len(compile_fns) == 1, "runtime policy 模块必须定义 compile_policy"
    compile_segment = ast.get_source_segment(source, compile_fns[0])
    assert compile_segment is not None
    assert "canonicalize_policy" in compile_segment, (
        "compile_policy 必须引用共享 canonicalize_policy，禁止维护第二套校验真源"
    )


# ---------------------------------------------------------------------------
# AC7：0013 迁移自包含（架构/AST 合同，不锁实现行号）
# ---------------------------------------------------------------------------


def test_migration_0013_is_self_contained_without_app_imports() -> None:
    """0013 迁移不得 import 任何 ``komari_bot`` 可变应用模块。

    迁移自包含（内联 digest/检查逻辑），禁止把运行时验证真源引入迁移链；
    只断言 import 依赖边界。
    """
    source = _module_source("migrations/versions/0013_group_admission_cutover.py")
    tree = ast.parse(source)

    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module is not None:
                imported_modules.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name)

    forbidden = sorted(
        module
        for module in imported_modules
        if module == "komari_bot" or module.startswith("komari_bot.")
    )
    assert forbidden == [], f"0013 迁移不得 import 应用模块: {forbidden}"
