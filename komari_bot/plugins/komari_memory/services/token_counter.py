"""Komari Memory 近似 token 计算工具。"""

from __future__ import annotations


def estimate_text_tokens(text: str) -> int:
    """按当前项目约定估算文本 token 数。

    目前沿用历史实现的近似口径：直接使用字符长度。
    这里集中封装，避免不同调用链各自维护一套估算规则。
    """
    return len(text)
