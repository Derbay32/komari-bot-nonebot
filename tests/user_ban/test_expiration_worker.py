"""user_ban 自然解封任务测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from komari_bot.plugins.user_ban import expiration_worker
from komari_bot.plugins.user_ban.models import BanRecord, NotificationResult


def _record(user_id: str, scope: str) -> BanRecord:
    timestamp = datetime(2026, 7, 13, 20, 0, tzinfo=UTC)
    return BanRecord(
        user_id=user_id,
        ban_scope=scope,  # type: ignore[arg-type]
        operator_id="42",
        reason="到期测试",
        expires_at=timestamp,
        created_at=timestamp,
        updated_at=timestamp,
    )


class _Service:
    async def expire_due_bans(self) -> tuple[BanRecord, ...]:
        return (
            _record("10086", "chat"),
            _record("10086", "command"),
            _record("10010", "chat"),
        )


@pytest.mark.asyncio
async def test_expiration_sweep_groups_records_by_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def notify(*_args: object, **kwargs: Any) -> NotificationResult:
        calls.append(kwargs)
        return NotificationResult(attempted=True, sent=True)

    monkeypatch.setattr(expiration_worker, "get_service", lambda: _Service())
    monkeypatch.setattr(expiration_worker, "get_first_available_bot", object)
    monkeypatch.setattr(expiration_worker, "notify_expired_records", notify)
    monkeypatch.setattr(
        expiration_worker,
        "is_configured_superuser_id",
        lambda user_id: user_id == "10010",
    )

    await expiration_worker.run_expiration_sweep()

    assert len(calls) == 2
    first = next(call for call in calls if call["user_id"] == "10086")
    second = next(call for call in calls if call["user_id"] == "10010")
    assert [record.ban_scope for record in first["records"]] == ["chat", "command"]
    assert first["superuser_bypass"] is False
    assert second["superuser_bypass"] is True
