"""项目共享的近似 token 计算工具。"""

from __future__ import annotations


def estimate_text_tokens(text: str) -> int:
    """按当前项目约定估算文本 token 数。"""
    return len(text)
