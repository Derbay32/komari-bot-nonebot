"""统一群聊准入策略的编译与解释（纯函数，无 I/O）。

策略载荷必须恰好是 ``{"mode": blacklist | whitelist, "group_ids": [正整数]}``，
模式与群号集合作为同一个策略修订原子出现。编译把合法载荷变为不可变的
``_CompiledPolicy``；非法载荷抛出 ``PolicyCompilationError``。异常消息与编
译产物均不携带策略正文或群号。

真值表（ADR-0012）：

- 黑名单模式空集合表示全部群获准，非空集合内受限、其余获准；
- 白名单模式空集合同样表示全部群获准（明确领域规则，不采用「空白名单
  拒绝全部」的直觉语义），非空仅集合内获准；
- 一次群行为必须全部关联群均获准，任一受限则整体受限。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from komari_bot.admission_policy import (
    POLICY_FILE_INVALID,
    PolicyCanonicalizationError,
    canonicalize_policy,
)

type PolicyMode = Literal["blacklist", "whitelist"]


class PolicyCompilationError(ValueError):
    """策略载荷编译失败；异常消息不包含策略正文或群号。"""


@dataclass(frozen=True, slots=True)
class _CompiledPolicy:
    """一个合法策略修订的不可变编译产物。"""

    mode: PolicyMode
    group_ids: frozenset[int]


def compile_policy(payload: object) -> _CompiledPolicy:
    """把策略载荷编译为不可变产物。

    TSK-247 起复用 ``komari_bot.admission_policy.canonicalize_policy`` 作为
    唯一校验真源（与 CLI / 0013 迁移 / 管理审计同一 canonical 规则），把
    共享校验异常安全映射到本模块既有的 ``PolicyCompilationError`` 契约：
    映射只剥离 cutover closed code 前缀，不携带任何策略正文或群号。
    """
    try:
        canonical = canonicalize_policy(payload)
    except PolicyCanonicalizationError as error:
        message = str(error).removeprefix(f"{POLICY_FILE_INVALID}: ")
        raise PolicyCompilationError(message) from None
    return _CompiledPolicy(
        mode=canonical["mode"],
        group_ids=frozenset(canonical["group_ids"]),
    )


def policy_admits(policy: _CompiledPolicy, associated_group_ids: frozenset[int]) -> bool:
    """按编译产物裁决全部关联群是否获准。

    空名单（黑/白同义）一律全部获准；黑名单非空时命中集合的关联群受
    限；白名单非空时仅集合内的关联群获准。任一关联群受限，整体行为受限。
    """
    if not policy.group_ids:
        return True
    if policy.mode == "blacklist":
        return not policy.group_ids.intersection(associated_group_ids)
    return associated_group_ids.issubset(policy.group_ids)
