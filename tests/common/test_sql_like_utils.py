"""SQL LIKE/ILIKE 工具测试。"""

from __future__ import annotations

from komari_bot.common.sql_like_utils import escape_like_pattern


def test_escape_like_pattern_escapes_wildcards_and_escape_char() -> None:
    assert escape_like_pattern("100%") == r"100\%"
    assert escape_like_pattern("a_b") == r"a\_b"
    assert escape_like_pattern(r"a\b") == r"a\\b"
    assert escape_like_pattern("%_\\") == "\\%\\_\\\\"


def test_escape_like_pattern_keeps_empty_string_empty() -> None:
    assert escape_like_pattern("") == ""
