"""group_history_summary scene 识别测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest


@pytest.mark.asyncio
async def test_summary_request_hits_summary_scene(monkeypatch: Any) -> None:
    import komari_bot.plugins.group_history_summary as summary_module

    async def _fake_rank_message(*_args: Any, **_kwargs: Any) -> object:
        return SimpleNamespace(
            best_scene_id="scene_group_history_summary",
            best_scene_score=0.82,
            meaningful_score=0.88,
            noise_score=0.11,
        )

    monkeypatch.setattr(
        summary_module._scene_rerank_service,
        "rank_message",
        _fake_rank_message,
    )

    assert await summary_module._is_summary_request("帮我总结一下今天群里主要聊了啥")


@pytest.mark.asyncio
async def test_summary_request_rejects_non_summary_scene(monkeypatch: Any) -> None:
    import komari_bot.plugins.group_history_summary as summary_module

    async def _fake_rank_message(*_args: Any, **_kwargs: Any) -> object:
        return SimpleNamespace(
            best_scene_id="scene_direct_interaction",
            best_scene_score=0.9,
            meaningful_score=0.8,
            noise_score=0.1,
        )

    monkeypatch.setattr(
        summary_module._scene_rerank_service,
        "rank_message",
        _fake_rank_message,
    )

    assert not await summary_module._is_summary_request("总结一下")
