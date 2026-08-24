"""统一群聊准入策略的共享 canonical 真值（纯函数，无 I/O、无插件依赖）。

TSK-232 轮 B 新建的 cutover 共享真源：CLI（``komari_bot.cutover``）、迁移
链与运行时插件（``komari_bot.plugins.group_admission.policy``，经本模块
re-export/薄封装复用）对同一载荷必须产出一致的 canonical 形态与指纹。

策略载荷必须恰好是 ``{"mode": blacklist | whitelist, "group_ids": [正整数]}``：

- ``mode`` 仅接受 ``blacklist`` / ``whitelist``；
- ``group_ids`` 必须是 list，元素必须是正整数 OneBot 群号
  （``type(x) is int`` 且 > 0，拒绝 bool、零、负数、字符串与浮点）；
- 编译时确定性去重并升序排序。

非法输入抛出 :class:`PolicyCanonicalizationError`；异常消息携带 closed
code ``POLICY_FILE_INVALID`` 且绝不回显任何输入值。canonical 形态要求
group_ids 升序去重；策略指纹直接对 canonical 升序存储形态取紧凑 JSON
（``sort_keys`` + ``separators=(",", ":")``）的 SHA-256 hex，与 0013 迁移
对存储 JSONB 文本重算 digest 的字节面逐字一致（序列约定由验收基线
锁定，详见 :func:`policy_fingerprint`）。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Never

#: mode 闭集（blacklist / whitelist）。
_POLICY_MODES = ("blacklist", "whitelist")

_POLICY_KEYS = frozenset(("mode", "group_ids"))

#: 非法载荷统一 closed code：错误面只携带该码与固定文案，绝不回显输入。
POLICY_FILE_INVALID = "POLICY_FILE_INVALID"


class PolicyCanonicalizationError(ValueError):
    """策略载荷 canonical 化失败；异常消息不含策略正文或群号。"""


def _reject(reason: str) -> Never:
    """以统一 closed code 抛错；reason 为固定文案，不携带动态输入。"""
    msg = f"{POLICY_FILE_INVALID}: {reason}"
    raise PolicyCanonicalizationError(msg)


def canonicalize_policy(payload: object) -> dict[str, Any]:
    """把策略载荷 canonical 化为恰好两键的字典形态。

    返回 ``{"mode": <str>, "group_ids": [升序去重正整数]}``；任何非法输入
    （非对象、键集合不符、mode 越界、群号非正整数列表）都抛
    :class:`PolicyCanonicalizationError`，异常消息只含 closed code 与固定
    文案，绝不回显输入内容。
    """
    if not isinstance(payload, dict):
        _reject("策略载荷必须是对象")
    if set(payload) != _POLICY_KEYS:
        _reject("策略载荷必须恰好包含 mode 与 group_ids")

    raw_mode = payload["mode"]
    if raw_mode not in _POLICY_MODES:
        _reject("策略 mode 必须是 blacklist 或 whitelist")

    raw_group_ids = payload["group_ids"]
    if not isinstance(raw_group_ids, list):
        _reject("策略 group_ids 必须是 list")

    group_ids: set[int] = set()
    for element in raw_group_ids:
        # type(x) is int 显式拒绝 bool 伪装（bool 是 int 子类）。
        if type(element) is not int or element <= 0:
            _reject("策略 group_ids 必须全部是正整数群号")
        group_ids.add(element)

    return {"mode": raw_mode, "group_ids": sorted(group_ids)}


def policy_fingerprint(payload: object) -> str:
    """先过 canonical 关，再对 canonical 存储形态取 SHA-256（64-hex）。

    指纹序列约定（验收基线字节级锁定）：直接对 canonical 升序去重的存储
    形态（``sort_keys`` + 紧凑分隔符）取摘要，与 0013 迁移对存储 JSONB
    文本重算 digest 的字节面逐字一致。该约定对群号顺序与重复不敏感
    （同一策略唯一指纹），并与验收支持层 ``oracle_fingerprint`` 对基准
    策略的取值逐字节一致。
    """
    canonical = canonicalize_policy(payload)
    serialized = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


__all__ = [
    "POLICY_FILE_INVALID",
    "PolicyCanonicalizationError",
    "canonicalize_policy",
    "policy_fingerprint",
]
