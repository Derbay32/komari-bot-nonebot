"""TSK-232 轮 B —— ``komari_bot.admission_policy`` canonical 真值纯单测（红基线）。

锁定共享包 ``komari_bot/admission_policy.py`` 的纯函数契约（无 DB、无
NoneBot 依赖）：

- ``canonicalize_policy``：mode 闭集、group_ids 正整数闭集（``type(x) is
  int`` 且 >0，拒绝 bool/零/负/字符串/浮点）、键恰好为 mode+group_ids、
  去重并升序排序；非法输入抛 ``PolicyCanonicalizationError``，消息含 closed
  code ``POLICY_FILE_INVALID`` 且绝不回显输入值；
- ``policy_fingerprint``：canonical 形态紧凑 JSON 的 SHA-256 hex，与
  ``reply_fulfillment_domain`` 的 payload_hash 规范化范式一致；同输入稳定、
  与群号顺序无关、内容差异敏感。

共享模块在轮 B 实现前不存在：用例以 ModuleNotFoundError 红基线失败，
本文件可正常收集、Ruff/Pyright 零错误。
"""

from __future__ import annotations

import hashlib
import importlib
import json
from typing import TYPE_CHECKING, Any

import pytest

from tests.cutover.support import (
    CANARY_GROUP_A,
    CANARY_GROUP_B,
    CANARY_GROUP_C,
    CANARY_LEAK_MARKER,
    VALID_POLICY,
    oracle_fingerprint,
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


def _fingerprint(policy: object) -> str:
    fingerprint: str = _admission_policy_module().policy_fingerprint(policy)
    return fingerprint


def _canonicalization_error() -> type[Exception]:
    error_type: type[Exception] = _admission_policy_module().PolicyCanonicalizationError
    return error_type


# ---------------------------------------------------------------------------
# canonicalize_policy 合法形态
# ---------------------------------------------------------------------------


def test_canonicalize_blacklist_keeps_exact_shape() -> None:
    """合法 blacklist 载荷 canonical 后恰好是两键形态。"""
    assert _canonicalize(VALID_POLICY) == {
        "mode": "blacklist",
        "group_ids": [CANARY_GROUP_A, CANARY_GROUP_B],
    }


def test_canonicalize_whitelist_accepts_mode() -> None:
    """whitelist 模式同样是合法闭集成员。"""
    payload = {"mode": "whitelist", "group_ids": [CANARY_GROUP_A]}
    assert _canonicalize(payload) == {
        "mode": "whitelist",
        "group_ids": [CANARY_GROUP_A],
    }


def test_canonicalize_deduplicates_and_sorts_group_ids() -> None:
    """重复与乱序群号被去重并升序排序。"""
    payload = {
        "mode": "blacklist",
        "group_ids": [CANARY_GROUP_B, CANARY_GROUP_A, CANARY_GROUP_B],
    }
    assert _canonicalize(payload)["group_ids"] == [
        CANARY_GROUP_A,
        CANARY_GROUP_B,
    ]


def test_canonicalize_empty_group_ids_is_valid() -> None:
    """空群名单（黑/白同义全放行）是合法 canonical 形态。"""
    for mode in ("blacklist", "whitelist"):
        payload = {"mode": mode, "group_ids": []}
        assert _canonicalize(payload) == {"mode": mode, "group_ids": []}


# ---------------------------------------------------------------------------
# canonicalize_policy 非法形态矩阵
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "allowall", "group_ids": []},
        {"mode": None, "group_ids": []},
        {"mode": "blacklist"},
        {"group_ids": []},
        {"mode": "blacklist", "group_ids": [], "extra": 1},
        {"mode": "blacklist", "group_ids": [True]},
        {"mode": "blacklist", "group_ids": [False]},
        {"mode": "blacklist", "group_ids": [0]},
        {"mode": "blacklist", "group_ids": [-CANARY_GROUP_A]},
        {"mode": "blacklist", "group_ids": [str(CANARY_GROUP_A)]},
        {"mode": "blacklist", "group_ids": [float(CANARY_GROUP_A)]},
        {"mode": "blacklist", "group_ids": [None]},
        {"mode": "blacklist", "group_ids": [CANARY_GROUP_A, 0]},
        {"mode": "blacklist", "group_ids": "123"},
    ],
    ids=[
        "mode-not-closed",
        "mode-none",
        "missing-group-ids",
        "missing-mode",
        "extra-key",
        "bool-true-masquerade",
        "bool-false-masquerade",
        "zero-group-id",
        "negative-group-id",
        "string-group-id",
        "float-group-id",
        "none-element",
        "mixed-invalid-element",
        "ids-not-a-list",
    ],
)
def test_canonicalize_rejects_invalid_payloads(payload: object) -> None:
    """全部非法形态统一抛 PolicyCanonicalizationError。"""
    with pytest.raises(_canonicalization_error()):
        _canonicalize(payload)


@pytest.mark.parametrize(
    "payload",
    [None, [], "policy", 42, ("blacklist", [])],
)
def test_canonicalize_rejects_non_object_payloads(payload: object) -> None:
    """非对象载荷（列表/字符串/标量）一律拒绝。"""
    with pytest.raises(_canonicalization_error()):
        _canonicalize(payload)


def test_canonicalization_error_carries_closed_code_only() -> None:
    """异常消息含 closed code POLICY_FILE_INVALID 且不回显输入值。

    用 canary 群号与标记串构造非法载荷，断言消息文本既不含 closed code
    以外的动态身份，也不回显任何输入片段。
    """
    leak_group_marker = f"{CANARY_LEAK_MARKER}"
    leak_payload: dict[str, Any] = {
        "mode": "blacklist",
        "group_ids": [leak_group_marker],
    }
    with pytest.raises(_canonicalization_error()) as exc_info:
        _canonicalize(leak_payload)
    message = str(exc_info.value)
    assert "POLICY_FILE_INVALID" in message
    assert CANARY_LEAK_MARKER not in message
    assert str(CANARY_GROUP_B) not in message


# ---------------------------------------------------------------------------
# policy_fingerprint
# ---------------------------------------------------------------------------


def test_fingerprint_matches_oracle_of_canonical_form() -> None:
    """fingerprint 等于 canonical 紧凑 JSON 的 SHA-256 hex oracle。"""
    fingerprint = _fingerprint(VALID_POLICY)
    assert fingerprint == oracle_fingerprint(VALID_POLICY)
    assert len(fingerprint) == 64
    int(fingerprint, 16)  # hex 可解析


def test_fingerprint_is_deterministic_for_same_input() -> None:
    """同输入两次调用产出完全一致。"""
    assert _fingerprint(VALID_POLICY) == _fingerprint(dict(VALID_POLICY))


def test_fingerprint_ignores_group_id_order_and_duplicates() -> None:
    """顺序不同/含重复的等价策略得到同一 fingerprint。"""
    ordered = _fingerprint(
        {"mode": "whitelist", "group_ids": [CANARY_GROUP_A, CANARY_GROUP_B]}
    )
    shuffled = _fingerprint(
        {"mode": "whitelist", "group_ids": [CANARY_GROUP_B, CANARY_GROUP_A]}
    )
    duplicated = _fingerprint(
        {
            "mode": "whitelist",
            "group_ids": [CANARY_GROUP_B, CANARY_GROUP_A, CANARY_GROUP_A],
        }
    )
    assert ordered == shuffled == duplicated


def test_fingerprint_distinguishes_content_and_mode() -> None:
    """群号或 mode 任一不同则 fingerprint 必然不同。"""
    base = _fingerprint(VALID_POLICY)
    other_group = _fingerprint({"mode": "blacklist", "group_ids": [CANARY_GROUP_C]})
    other_mode = _fingerprint(
        {"mode": "whitelist", "group_ids": list(VALID_POLICY["group_ids"])}
    )
    assert base != other_group
    assert base != other_mode


def test_fingerprint_serialization_matches_reference_convention() -> None:
    """规范化范式与 reply_fulfillment payload_hash 同构：sort_keys+紧凑分隔符。"""
    expected = hashlib.sha256(
        json.dumps(
            {"mode": "blacklist", "group_ids": [CANARY_GROUP_A]},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert _fingerprint({"mode": "blacklist", "group_ids": [CANARY_GROUP_A]}) == (
        expected
    )


def test_fingerprint_rejects_invalid_payload() -> None:
    """非法载荷不做指纹——先过 canonical 关。"""
    with pytest.raises(_canonicalization_error()):
        _fingerprint({"mode": "blacklist", "group_ids": [-1]})
