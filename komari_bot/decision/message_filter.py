"""消息过滤契约：过滤结果。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class FilterResult:
    """过滤结果。"""

    should_skip: bool
    reason: Literal["short", "history_repeat", "none", "command"]

    def __init__(
        self,
        *,
        should_skip: bool,
        reason: Literal["short", "history_repeat", "none", "command"],
    ) -> None:
        """初始化过滤结果（强制使用关键字参数）。

        Args:
            should_skip: 是否应该跳过BERT评分
            reason: 过滤原因
        """
        object.__setattr__(self, "should_skip", should_skip)
        object.__setattr__(self, "reason", reason)


__all__ = ["FilterResult"]
