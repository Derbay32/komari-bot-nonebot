"""权限管理工具函数测试。"""

from __future__ import annotations

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
from nonebot.adapters.onebot.v11.event import Sender

from komari_bot.plugins.permission_manager.utils import get_user_nickname


def _build_group_event(*, card: str | None, nickname: str | None) -> GroupMessageEvent:
    message = Message(".test")
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
        raw_message=".test",
        font=14,
        sender=Sender.model_construct(
            user_id=1047195267,
            nickname=nickname,
            card=card,
        ),
        to_me=False,
        reply=None,
        group_id=114514,
        anonymous=None,
    )


def test_get_user_nickname_prefers_card() -> None:
    event = _build_group_event(card="群名片", nickname="昵称")

    assert get_user_nickname(event) == "群名片"


def test_get_user_nickname_uses_nickname_without_card() -> None:
    event = _build_group_event(card="", nickname="昵称")

    assert get_user_nickname(event) == "昵称"


def test_get_user_nickname_falls_back_to_user_id() -> None:
    event = _build_group_event(card="", nickname="")

    assert get_user_nickname(event) == "用户（1047195267）"
