"""知识库写模型内容预算测试。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from komari_bot.plugins.komari_knowledge.models import (
    KnowledgeCreateRequest,
    KnowledgeEntry,
    KnowledgeSearchRequest,
    KnowledgeUpdateRequest,
)


@pytest.mark.parametrize(
    ("payload", "error_pattern"),
    [
        ({"content": "测" * 6_001, "keywords": ["测试"]}, "估算 token 上限"),
        ({"content": "内容", "keywords": ["词" * 129]}, "单个关键词"),
        ({"content": "内容", "keywords": [str(i) for i in range(21)]}, "关键词数量"),
        ({"content": "内容", "keywords": ["测试"], "notes": "注" * 2_001}, "备注"),
    ],
)
def test_create_request_rejects_each_budget_dimension(
    payload: dict[str, object],
    error_pattern: str,
) -> None:
    with pytest.raises(ValidationError, match=error_pattern):
        KnowledgeCreateRequest.model_validate(payload)


def test_update_and_search_requests_use_same_budget() -> None:
    with pytest.raises(ValidationError, match="估算 token 上限"):
        KnowledgeUpdateRequest(content="测" * 6_001)
    with pytest.raises(ValidationError, match="查询文本超过"):
        KnowledgeSearchRequest(query="查" * 513)


def test_read_model_accepts_historical_oversized_content() -> None:
    entry = KnowledgeEntry(
        id=1,
        category="general",
        keywords=["历史"],
        content="旧" * 20_000,
        notes="旧备注" * 2_000,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    assert len(entry.content) == 20_000
