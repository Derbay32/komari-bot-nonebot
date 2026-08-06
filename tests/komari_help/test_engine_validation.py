"""HelpEngine 直调入口内容预算测试。"""

from __future__ import annotations

import pytest

from komari_bot.common.content_budget import ContentValidationError
from komari_bot.plugins.komari_help.engine import HelpEngine


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
