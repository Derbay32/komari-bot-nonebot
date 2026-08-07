"""user_ban 管理命令测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from importlib import import_module
from typing import TYPE_CHECKING, Any, cast

import pytest
from nonebot.adapters.onebot.v11 import Adapter, Bot, Message, PrivateMessageEvent
from nonebot.adapters.onebot.v11.event import Sender

from komari_bot.onebot.onebot_messages import plain_text_message
from komari_bot.plugins.user_ban.models import (
    BanMutationResult,
    BanRecord,
    NotificationResult,
    UserBanStatus,
)

if TYPE_CHECKING:
    from nonebug import App


@pytest.fixture
def commands_module(app: App) -> Any:
    del app
    return import_module("komari_bot.plugins.user_ban.commands")


class _Service:
    def __init__(self) -> None:
        self.ban_calls: list[dict[str, object]] = []

    async def ban_user(self, **kwargs: object) -> BanMutationResult:
        self.ban_calls.append(dict(kwargs))
        timestamp = datetime(2026, 7, 13, 20, 0, tzinfo=UTC)
        record = BanRecord(
            user_id=cast("str", kwargs["user_id"]),
            ban_scope="chat",
            operator_id="42",
            reason=cast("str | None", kwargs["reason"]),
            expires_at=cast("datetime | None", kwargs["expires_at"]),
            created_at=timestamp,
            updated_at=timestamp,
        )
        return BanMutationResult(
            status=UserBanStatus(user_id=record.user_id, records=(record,)),
            target_scope=cast("Any", kwargs["target_scope"]),
            changed=True,
            mutation_kind="created",
            affected_records=(record,),
        )


def _event(text: str, *, user_id: int) -> PrivateMessageEvent:
    message = Message(text)
    return PrivateMessageEvent.model_construct(
        time=1,
        self_id=669293859,
        post_type="message",
        sub_type="friend",
        user_id=user_id,
        message_type="private",
        message_id=1,
        message=message,
        original_message=message,
        raw_message=text,
        font=14,
        sender=Sender.model_construct(user_id=user_id, nickname="tester", card=""),
        to_me=True,
        reply=None,
    )


def _bot(ctx: Any) -> Bot:
    adapter = ctx.create_adapter(base=Adapter)
    return cast("Bot", ctx.create_bot(base=Bot, adapter=adapter, self_id="669293859"))


@pytest.mark.asyncio
async def test_non_superuser_is_rejected(
    app: App,
    commands_module: Any,
) -> None:
    async with app.test_matcher(commands_module.ban_matcher) as ctx:
        bot = _bot(ctx)
        event = _event(".ban chat 10086", user_id=10000)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule(matcher=commands_module.ban_matcher)
        ctx.should_call_send(event, "❌ 仅限 SUPERUSER 使用", bot=bot)
        ctx.should_finished()


@pytest.mark.asyncio
async def test_superuser_can_ban_chat_scope_with_legacy_format(
    app: App,
    commands_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _Service()

    async def notify(*_args: object, **_kwargs: object) -> NotificationResult:
        return NotificationResult(attempted=True, sent=True)

    monkeypatch.setattr(commands_module, "get_service", lambda: service)
    monkeypatch.setattr(commands_module, "notify_ban_result", notify)

    async with app.test_matcher(commands_module.ban_matcher) as ctx:
        bot = _bot(ctx)
        event = _event(".ban chat 10086", user_id=42)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule(matcher=commands_module.ban_matcher)
        ctx.should_call_send(
            event,
            plain_text_message(
                "✅ 用户 10086 的聊天回复权限已封禁。\n"
                "当前状态：chat\n私信：已发送"
            ),
            bot=bot,
        )
        ctx.should_finished()

    assert service.ban_calls == [
        {
            "user_id": "10086",
            "target_scope": "chat",
            "operator_id": "42",
            "expires_at": None,
            "reason": None,
        }
    ]


@pytest.mark.asyncio
async def test_superuser_can_set_duration_and_reason(
    app: App,
    commands_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _Service()

    async def notify(*_args: object, **_kwargs: object) -> NotificationResult:
        return NotificationResult(attempted=True, sent=True)

    monkeypatch.setattr(commands_module, "get_service", lambda: service)
    monkeypatch.setattr(commands_module, "notify_ban_result", notify)

    async with app.test_matcher(commands_module.ban_matcher) as ctx:
        bot = _bot(ctx)
        event = _event(".ban chat 10086 7d 刷屏 广告", user_id=42)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule(matcher=commands_module.ban_matcher)
        ctx.should_call_send(
            event,
            plain_text_message(
                "✅ 用户 10086 的聊天回复权限已封禁。\n"
                "当前状态：chat\n私信：已发送"
            ),
            bot=bot,
        )
        ctx.should_finished()

    call = service.ban_calls[0]
    assert call["reason"] == "刷屏 广告"
    assert isinstance(call["expires_at"], datetime)


def test_duration_and_reason_parser(commands_module: Any) -> None:
    scope, user_id, expires_at, reason = commands_module._parse_ban_args(
        ["all", "10086", "2h", "多次", "刷屏"]
    )
    assert scope == "all"
    assert user_id == "10086"
    assert expires_at is not None
    assert reason == "多次 刷屏"

    permanent = commands_module._parse_ban_args(
        ["chat", "10086", "permanent", "长期观察"]
    )
    assert permanent[2] is None
    assert permanent[3] == "长期观察"

    with pytest.raises(ValueError, match="封禁时长"):
        commands_module._parse_ban_args(["chat", "10086", "1y"])
    with pytest.raises(ValueError, match="500"):
        commands_module._parse_ban_args(
            ["chat", "10086", "permanent", "理" * 501]
        )


def test_list_argument_parser(commands_module: Any) -> None:
    assert commands_module._parse_list_args([]) == (None, 1)
    assert commands_module._parse_list_args(["chat", "2"]) == ("chat", 2)
    assert commands_module._parse_list_args(["all", "3"]) == (None, 3)
    assert commands_module._parse_list_args(["2"]) == (None, 2)
    assert commands_module._parse_list_args(["bad"]) is None


def test_qq_user_id_validation(commands_module: Any) -> None:
    assert commands_module.normalize_qq_user_id("10086") == "10086"
    assert commands_module.normalize_qq_user_id("010086") is None
    assert commands_module.normalize_qq_user_id("abc") is None
