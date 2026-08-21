"""TSK-223 测试专用敏感金丝雀扫描器（基础工具，阶段 B 可扩展）。

验收语义（冻结）：

- 已鉴权 ``GET /api/v2/group-admission/policy`` 响应是 **唯一** 允许回显
  策略正文 / 群号的面，且只允许出现在精确路径 ``policy.group_ids`` 子树；
- 管理错误响应体、审计事件、状态投影、ETag 等任何其他面出现金丝雀值都
  构成泄漏（测试必须红）。

``SensitiveCanaryBundle`` 递归扫描任意 mapping / sequence / set /
dataclass / Pydantic 模型 / 字符串 / 字节组合：

- 字符串 token 以子串匹配任何字符串叶子（含字节解码与标量 ``str()`` 投影）；
- 整数 token 以相等匹配任何整数叶子（bool 除外），用于群号回显断言；
- 路径表示为字段名/索引字符串元组，``allowed_path_prefixes`` 按前缀子树
  放行（如 ``("policy", "group_ids")`` 放行该列表内全部元素）。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from pydantic import BaseModel


@dataclass(frozen=True, slots=True)
class SensitiveCanaryToken:
    """一个敏感金丝雀值；``label`` 只用于泄漏报告定位。"""

    label: str
    value: str | int


class SensitiveCanaryBundle:
    """一组敏感金丝雀与递归泄漏扫描器。"""

    def __init__(self, tokens: Iterable[SensitiveCanaryToken]) -> None:
        token_tuple = tuple(tokens)
        if not token_tuple:
            msg = "金丝雀 bundle 至少需要一个 token"
            raise ValueError(msg)
        labels = [token.label for token in token_tuple]
        if len(labels) != len(set(labels)):
            msg = f"金丝雀 label 必须唯一: {labels}"
            raise ValueError(msg)
        if any(token.value in ("", 0) for token in token_tuple):
            msg = "金丝雀值不得为空（会匹配一切）"
            raise ValueError(msg)
        self._tokens = token_tuple

    @property
    def tokens(self) -> tuple[SensitiveCanaryToken, ...]:
        return self._tokens

    def leak_report(
        self,
        payload: object,
        *,
        allowed_path_prefixes: frozenset[tuple[str, ...]] = frozenset(),
    ) -> list[tuple[str, tuple[str, ...]]]:
        """返回 ``(label, path)`` 泄漏清单；放行前缀子树内的命中不计。"""
        leaks: list[tuple[str, tuple[str, ...]]] = []
        self._walk(payload, (), allowed_path_prefixes, leaks)
        return leaks

    def assert_no_leaks(
        self,
        payload: object,
        *,
        allowed_path_prefixes: frozenset[tuple[str, ...]] = frozenset(),
        context: str = "payload",
    ) -> None:
        """断言无泄漏；失败消息只含 label 与路径，不复述金丝雀正文。"""
        leaks = self.leak_report(
            payload, allowed_path_prefixes=allowed_path_prefixes
        )
        assert leaks == [], f"{context} 检测到敏感金丝雀泄漏: {leaks}"

    # ------------------------------ 内部遍历 ------------------------------

    def _is_allowed(
        self,
        path: tuple[str, ...],
        allowed_path_prefixes: frozenset[tuple[str, ...]],
    ) -> bool:
        return any(
            path[: len(prefix)] == prefix for prefix in allowed_path_prefixes
        )

    def _match_leaf(
        self,
        leaf: object,
        path: tuple[str, ...],
        allowed_path_prefixes: frozenset[tuple[str, ...]],
        leaks: list[tuple[str, tuple[str, ...]]],
    ) -> None:
        if self._is_allowed(path, allowed_path_prefixes):
            return
        if isinstance(leaf, bool):
            return
        if isinstance(leaf, int):
            leaks.extend(
                (token.label, path)
                for token in self._tokens
                if isinstance(token.value, int) and leaf == token.value
            )
            return
        if isinstance(leaf, bytes):
            text = leaf.decode("utf-8", errors="replace")
        elif isinstance(leaf, str):
            text = leaf
        else:
            text = str(leaf)
        leaks.extend(
            (token.label, path)
            for token in self._tokens
            if isinstance(token.value, str) and token.value in text
        )

    def _walk(
        self,
        value: object,
        path: tuple[str, ...],
        allowed_path_prefixes: frozenset[tuple[str, ...]],
        leaks: list[tuple[str, tuple[str, ...]]],
    ) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                key_path = (*path, str(key))
                self._match_leaf(key, key_path, allowed_path_prefixes, leaks)
                self._walk(item, key_path, allowed_path_prefixes, leaks)
            return
        if isinstance(value, BaseModel):
            self._walk(
                value.model_dump(mode="python"),
                path,
                allowed_path_prefixes,
                leaks,
            )
            return
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            for field in dataclasses.fields(value):
                self._walk(
                    getattr(value, field.name),
                    (*path, field.name),
                    allowed_path_prefixes,
                    leaks,
                )
            return
        if isinstance(value, (set, frozenset)):
            for index, item in enumerate(sorted(value, key=repr)):
                self._walk(
                    item, (*path, str(index)), allowed_path_prefixes, leaks
                )
            return
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for index, item in enumerate(value):
                self._walk(
                    item, (*path, str(index)), allowed_path_prefixes, leaks
                )
            return
        self._match_leaf(value, path, allowed_path_prefixes, leaks)


def build_standard_canary_bundle() -> SensitiveCanaryBundle:
    """标准泄漏探针集：URL / base64 / CQ 码 / Bearer token / 换行正文。"""
    return SensitiveCanaryBundle(
        (
            SensitiveCanaryToken(
                label="url", value="https://canary.example/exfil?key=9f3a7c"
            ),
            SensitiveCanaryToken(
                label="base64", value="Q0FOQVJZLUI2NC1TRUNSRVQtOTZhMQ=="
            ),
            SensitiveCanaryToken(
                label="cq-code", value="[CQ:image,file=canary-77ab19.jpg]"
            ),
            SensitiveCanaryToken(
                label="bearer-token",
                value="Bearer canary-token-0123456789abcdef",
            ),
            SensitiveCanaryToken(
                label="newline-body",
                value="canary-line-1\nCANARY-NL-5e0d\nline-3",
            ),
        )
    )
