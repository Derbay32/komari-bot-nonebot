"""帮助库写模型内容预算测试。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from komari_bot.plugins.komari_help.models import (
    HelpCreateRequest,
    HelpEntry,
    HelpSearchRequest,
    HelpUpdateRequest,
)


@pytest.mark.parametrize(
    ("payload", "error_pattern"),
    [
        ({"title": "题" * 129, "content": "内容"}, "帮助标题"),
        ({"title": "标题", "content": "测" * 6_001}, "估算 token 上限"),
        ({"title": "标题", "content": "内容", "keywords": ["词" * 129]}, "单个关键词"),
        ({"title": "标题", "content": "内容", "plugin_name": "p" * 129}, "插件名"),
        ({"title": "标题", "content": "内容", "notes": "注" * 2_001}, "备注"),
    ],
)
def test_create_request_rejects_each_budget_dimension(
    payload: dict[str, object],
    error_pattern: str,
) -> None:
    with pytest.raises(ValidationError, match=error_pattern):
        HelpCreateRequest.model_validate(payload)


def test_update_and_search_requests_use_same_budget() -> None:
    with pytest.raises(ValidationError, match="帮助内容"):
        HelpUpdateRequest(content="测" * 6_001)
    with pytest.raises(ValidationError, match="查询文本超过"):
        HelpSearchRequest(query="查" * 513)


def test_read_model_accepts_historical_oversized_fields() -> None:
    entry = HelpEntry(
        id=1,
        category="other",
        plugin_name="p" * 500,
        keywords=["历史"],
        title="旧" * 500,
        content="旧" * 20_000,
        notes="旧备注" * 2_000,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    assert len(entry.title) == 500
    assert len(entry.content) == 20_000
