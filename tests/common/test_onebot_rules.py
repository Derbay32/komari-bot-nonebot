"""OneBot 事件规则测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, PrivateMessageEvent
from nonebot.adapters.onebot.v11.event import Sender

from komari_bot.common.onebot_rules import (
    group_message_rule,
    group_message_to_me_rule,
)


def _build_sender(user_id: int) -> Sender:
    return Sender.model_construct(user_id=user_id, nickname="tester")


def _build_group_event(*, to_me: bool = False) -> GroupMessageEvent:
    message = Message(".r")
    return GroupMessageEvent.model_construct(
        time=1,
        self_id=669293859,
        post_type="message",
        sub_type="normal",
        user_id=1047195267,
        message_type="group",
        message_id=123,
        message=message,
        original_message=message,
        raw_message=".r",
        font=14,
        sender=_build_sender(1047195267),
        to_me=to_me,
        reply=None,
        group_id=114514,
        anonymous=None,
    )


def _build_private_event(*, to_me: bool = True) -> PrivateMessageEvent:
    message = Message(".r")
    return PrivateMessageEvent.model_construct(
        time=1,
        self_id=669293859,
        post_type="message",
        sub_type="friend",
        user_id=1047195267,
        message_type="private",
        message_id=123,
        message=message,
        original_message=message,
        raw_message=".r",
        font=14,
        sender=_build_sender(1047195267),
        to_me=to_me,
        reply=None,
    )


def _run_rule(rule: object, event: object) -> bool:
    return asyncio.run(rule(SimpleNamespace(), event, {}))  # type: ignore[misc]


def test_group_message_rule_accepts_group_event() -> None:
    assert _run_rule(group_message_rule(), _build_group_event()) is True


def test_group_message_rule_rejects_private_event() -> None:
    assert _run_rule(group_message_rule(), _build_private_event()) is False


def test_group_message_to_me_rule_accepts_to_me_group_event() -> None:
    assert _run_rule(group_message_to_me_rule(), _build_group_event(to_me=True)) is True


def test_group_message_to_me_rule_rejects_private_event_even_if_to_me() -> None:
    assert _run_rule(group_message_to_me_rule(), _build_private_event(to_me=True)) is False
