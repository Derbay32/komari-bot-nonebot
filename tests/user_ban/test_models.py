"""user_ban 输入模型测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from komari_bot.plugins.user_ban.models import (
    normalize_ban_reason,
    parse_ban_duration,
)


def test_parse_supported_durations_and_permanent() -> None:
    now = datetime(2026, 7, 13, 20, 0, tzinfo=UTC)

    assert parse_ban_duration(None, now=now) is None
    assert parse_ban_duration("permanent", now=now) is None
    assert parse_ban_duration("1m", now=now) == now + timedelta(minutes=1)
    assert parse_ban_duration("2h", now=now) == now + timedelta(hours=2)
    assert parse_ban_duration("3d", now=now) == now + timedelta(days=3)
    assert parse_ban_duration("4w", now=now) == now + timedelta(weeks=4)


def test_duration_and_reason_limits() -> None:
    with pytest.raises(ValueError, match="十年"):
        parse_ban_duration("522w")
    with pytest.raises(ValueError, match="十年"):
        parse_ban_duration("9" * 100 + "w")
    with pytest.raises(ValueError, match="必须为"):
        parse_ban_duration("0m")
    with pytest.raises(ValueError, match="500"):
        normalize_ban_reason("理" * 501)

    assert normalize_ban_reason("  测试理由  ") == "测试理由"
    assert normalize_ban_reason("   ") is None
