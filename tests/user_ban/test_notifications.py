"""user_ban 私信通知测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import pytest

from komari_bot.plugins.user_ban.models import (
    BanMutationResult,
    BanRecord,
    UserBanStatus,
)
from komari_bot.plugins.user_ban.notifications import (
    notify_ban_result,
    notify_expired_records,
    notify_unban_result,
)

if TYPE_CHECKING:
    from nonebot.adapters.onebot.v11 import Message


def _record(scope: str = "chat") -> BanRecord:
    timestamp = datetime(2026, 7, 13, 20, 0, tzinfo=UTC)
    return BanRecord(
        user_id="10086",
        ban_scope=cast("Any", scope),
        operator_id="42",
        reason="刷屏广告",
        expires_at=datetime.now(UTC) + timedelta(days=7),
        created_at=timestamp,
        updated_at=timestamp,
    )


class _Bot:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def call_api(self, name: str, **kwargs: object) -> object:
        self.calls.append({"name": name, **kwargs})
        if self.error is not None:
            raise self.error
        return {"message_id": 1}


@pytest.mark.asyncio
async def test_ban_notification_uses_plain_private_message() -> None:
    record = _record()
    result = BanMutationResult(
        status=UserBanStatus(user_id="10086", records=(record,)),
        target_scope="chat",
        changed=True,
        mutation_kind="created",
        affected_records=(record,),
    )
    bot = _Bot()

    delivery = await notify_ban_result(
        cast("Any", bot),
        result,
        superuser_bypass=False,
    )

    assert delivery.sent is True
    assert bot.calls[0]["name"] == "send_private_msg"
    assert bot.calls[0]["user_id"] == 10086
    message = cast("Message", bot.calls[0]["message"])
    assert len(message) == 1
    assert message[0].type == "text"
    text = str(message[0].data["text"])
    assert "封禁通知" in text
    assert "刷屏广告" in text
    assert "期限" in text


@pytest.mark.asyncio
async def test_unban_and_expiry_notifications_preserve_original_reason() -> None:
    records = (_record("chat"), _record("command"))
    result = BanMutationResult(
        status=UserBanStatus(user_id="10086"),
        target_scope="all",
        changed=True,
        mutation_kind="removed",
        affected_records=records,
    )
    bot = _Bot()

    manual = await notify_unban_result(
        cast("Any", bot),
        result,
        superuser_bypass=False,
    )
    natural = await notify_expired_records(
        cast("Any", bot),
        user_id="10086",
        records=records,
        superuser_bypass=True,
    )

    assert manual.sent is True
    assert natural.sent is True
    assert len(bot.calls) == 2
    manual_message = cast("Message", bot.calls[0]["message"])
    natural_message = cast("Message", bot.calls[1]["message"])
    assert "管理员解除" in str(manual_message[0].data["text"])
    assert "自然到期" in str(natural_message[0].data["text"])
    assert "SUPERUSER" in str(natural_message[0].data["text"])


@pytest.mark.asyncio
async def test_notification_failure_does_not_raise_or_retry() -> None:
    record = _record()
    result = BanMutationResult(
        status=UserBanStatus(user_id="10086", records=(record,)),
        target_scope="chat",
        changed=True,
        mutation_kind="updated",
        affected_records=(record,),
    )
    bot = _Bot(RuntimeError("风控拒绝"))

    delivery = await notify_ban_result(
        cast("Any", bot),
        result,
        superuser_bypass=False,
    )

    assert delivery.attempted is True
    assert delivery.sent is False
    assert delivery.error == "平台私信发送失败"
    assert len(bot.calls) == 1


@pytest.mark.asyncio
async def test_unchanged_operation_and_offline_bot_do_not_call_api() -> None:
    unchanged = BanMutationResult(
        status=UserBanStatus(user_id="10086"),
        target_scope="chat",
        changed=False,
    )

    skipped = await notify_ban_result(
        None,
        unchanged,
        superuser_bypass=False,
    )
    offline = await notify_expired_records(
        None,
        user_id="10086",
        records=(_record(),),
        superuser_bypass=False,
    )

    assert skipped.attempted is False
    assert skipped.error is None
    assert offline.attempted is False
    assert offline.error == "Bot 不在线，无法发送私信"
