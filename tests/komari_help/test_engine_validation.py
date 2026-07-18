"""HelpEngine 直调入口内容预算测试。"""

from __future__ import annotations

from typing import Self

import pytest

from komari_bot.common.content_budget import ContentValidationError
from komari_bot.plugins.komari_help.engine import HelpEngine


class _UpdatePool:
    def __init__(self) -> None:
        self.fetchval_calls: list[tuple[str, tuple[object, ...]]] = []
        self.active_acquisitions = 0

    def acquire(self) -> _UpdatePool:
        return self

    async def __aenter__(self) -> Self:
        self.active_acquisitions += 1
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.active_acquisitions -= 1
        del exc_type, exc, tb

    async def fetchrow(self, _query: str, hid: int) -> dict[str, object]:
        assert hid == 1
        return {
            "title": "旧标题",
            "content": "旧内容",
            "keywords": ["旧词"],
            "category": "other",
            "plugin_name": "demo",
            "notes": None,
            "row_version": "42",
        }

    async def fetchval(self, query: str, *args: object) -> int:
        self.fetchval_calls.append((query, args))
        return 1


@pytest.mark.asyncio
async def test_add_help_rejects_budget_before_embedding() -> None:
    engine = HelpEngine()
    engine._pool = object()
    embedding_called = False

    async def _unexpected_embedding(_text: str) -> list[float]:
        nonlocal embedding_called
        embedding_called = True
        return []

    engine._get_embedding = _unexpected_embedding  # type: ignore[method-assign]

    with pytest.raises(ContentValidationError, match="帮助标题超过"):
        await engine.add_help(
            title="题" * 129,
            content="内容",
            keywords=["帮助"],
        )

    assert embedding_called is False


@pytest.mark.asyncio
async def test_update_help_embeds_and_writes_normalized_text() -> None:
    engine = HelpEngine()
    pool = _UpdatePool()
    engine._pool = pool
    embedding_inputs: list[str] = []

    async def _embedding(text: str) -> list[float]:
        assert pool.active_acquisitions == 0
        embedding_inputs.append(text)
        return [0.1, 0.2]

    async def _ignore_rebuild() -> None:
        return None

    engine._get_embedding = _embedding  # type: ignore[method-assign]
    engine._build_keyword_index = _ignore_rebuild  # type: ignore[method-assign]

    updated = await engine.update_help(1, title="  新标题  ")

    assert updated is True
    assert embedding_inputs == ["新标题\n旧内容"]
    query, args = pool.fetchval_calls[0]
    assert "title = $3" in query
    assert "xmin::text = $2" in query
    assert args == (1, "42", "新标题", str([0.1, 0.2]))
