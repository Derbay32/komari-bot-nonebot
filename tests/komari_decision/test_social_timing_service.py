"""SocialTimingService 单元测试。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast

from komari_bot.plugins.komari_decision.services import social_timing_service as sts
from komari_bot.plugins.komari_decision.services.social_timing_service import (
    SocialTimingService,
)


@dataclass
class DummyMessage:
    user_id: str
    timestamp: float
    is_bot: bool = False


class DummyRedis:
    def __init__(self, messages: list[DummyMessage]) -> None:
        self._messages = messages

    async def get_buffer(self, group_id: str, limit: int = 100) -> list[DummyMessage]:
        del group_id, limit
        return list(self._messages)


def _patch_config(monkeypatch: Any) -> None:
    config = SimpleNamespace(
        message_buffer_size=200,
        social_window_activity_seconds=10,
        social_window_dialogue_seconds=30,
        social_silence_seconds=60,
        social_bot_cooldown_seconds=10,
    )
    monkeypatch.setattr(sts, "get_config", lambda: config)


def test_score_silence_bonus_when_empty(monkeypatch: Any) -> None:
    _patch_config(monkeypatch)
    service = SocialTimingService(cast("Any", DummyRedis(messages=[])))
    result = asyncio.run(service.score("123", alias_hit=False, now_ts=1000.0))
    assert result.silence_bonus == 0.2
    assert result.activity_penalty == 0.0
    assert result.cooldown_penalty == 0.0
    assert result.timing_score == 0.2


def test_score_activity_penalty(monkeypatch: Any) -> None:
    _patch_config(monkeypatch)
    now = 2000.0
    messages = [
        DummyMessage(user_id=str(i % 3), timestamp=now - 1.0, is_bot=False)
        for i in range(8)
    ]
    service = SocialTimingService(cast("Any", DummyRedis(messages=messages)))
    result = asyncio.run(service.score("123", alias_hit=False, now_ts=now))
    assert result.activity_count == 8
    assert result.activity_penalty > 0.0
    assert result.timing_score < 1.0


def test_score_bot_cooldown_penalty(monkeypatch: Any) -> None:
    _patch_config(monkeypatch)
    now = 3000.0
    messages = [
        DummyMessage(user_id="u1", timestamp=now - 2.0, is_bot=True),
    ]
    service = SocialTimingService(cast("Any", DummyRedis(messages=messages)))
    result = asyncio.run(service.score("123", alias_hit=True, now_ts=now))
    assert result.mention_bonus == 0.2
    assert result.cooldown_penalty > 0.0
    assert result.bot_gap_seconds == 2.0
