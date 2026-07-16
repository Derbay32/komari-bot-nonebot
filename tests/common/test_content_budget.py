"""统一内容预算测试。"""

from __future__ import annotations

import pytest

from komari_bot.common.content_budget import (
    MAX_KEYWORD_COUNT,
    ContentValidationError,
    TextBudget,
    estimate_text_tokens,
    normalize_keywords,
    normalize_optional_text,
    normalize_required_text,
    validate_text_budget,
)


def test_required_and_optional_text_are_normalized() -> None:
    budget = TextBudget(20, 60, 20)

    assert normalize_required_text("  正文  ", label="内容", budget=budget) == "正文"
    assert normalize_optional_text("   ", label="备注", budget=budget) is None

    with pytest.raises(ContentValidationError, match="内容不能为空"):
        normalize_required_text("   ", label="内容", budget=budget)


def test_text_budget_checks_characters_utf8_bytes_and_tokens() -> None:
    with pytest.raises(ContentValidationError, match="字符上限"):
        validate_text_budget(
            "abcd",
            label="字段",
            budget=TextBudget(3, 100, 100),
        )

    with pytest.raises(ContentValidationError, match="UTF-8 字节上限"):
        validate_text_budget(
            "你好",
            label="字段",
            budget=TextBudget(10, 5, 100),
        )

    with pytest.raises(ContentValidationError, match="估算 token 上限"):
        validate_text_budget(
            "中文测",
            label="字段",
            budget=TextBudget(10, 30, 2),
        )


def test_invalid_unicode_is_rejected_without_echoing_content() -> None:
    with pytest.raises(ContentValidationError, match="无效 Unicode 字符") as exc_info:
        validate_text_budget(
            "prefix\ud800secret",
            label="字段",
            budget=TextBudget(100, 100, 100),
        )

    assert "secret" not in str(exc_info.value)


def test_keyword_budget_normalizes_deduplicates_and_limits_raw_count() -> None:
    assert normalize_keywords(
        [" Alpha ", "alpha", "", "测试"],
        require_nonempty=True,
    ) == ["Alpha", "测试"]

    with pytest.raises(ContentValidationError, match="关键词数量超过上限"):
        normalize_keywords(
            ["词"] * (MAX_KEYWORD_COUNT + 1),
            require_nonempty=False,
        )

    with pytest.raises(ContentValidationError, match="单个关键词超过字符上限"):
        normalize_keywords(["词" * 129], require_nonempty=True)

    with pytest.raises(ContentValidationError, match="关键词不能为空"):
        normalize_keywords(["", "   "], require_nonempty=True)


def test_token_estimate_is_conservative_for_chinese_and_ascii() -> None:
    assert estimate_text_tokens("测" * 10) == 10
    assert estimate_text_tokens("a" * 12) == 4
