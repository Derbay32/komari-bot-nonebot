"""SQL LIKE/ILIKE 查询工具。"""

from __future__ import annotations


def escape_like_pattern(value: str, escape_char: str = "\\") -> str:
    """转义 LIKE/ILIKE pattern 中的通配符与转义符。"""
    if not value:
        return ""

    return (
        value.replace(escape_char, escape_char * 2)
        .replace("%", f"{escape_char}%")
        .replace("_", f"{escape_char}_")
    )
