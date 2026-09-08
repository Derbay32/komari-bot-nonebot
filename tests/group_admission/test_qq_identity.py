"""TSK-274 trusted QQ identity split for the user-ban boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from nonebot.adapters.qq.event import GroupAtMessageCreateEvent
from nonebot.adapters.qq.models.qq import GroupMemberAuthor

from komari_bot.plugins.user_ban import event_support

if TYPE_CHECKING:
    from nonebot.adapters import Bot, Event

pytestmark = pytest.mark.group_admission_acceptance


def _qq_group_at(member_openid: str) -> GroupAtMessageCreateEvent:
    author = GroupMemberAuthor.model_construct(
        id="qq-member-id",
        bot=False,
        member_openid=member_openid,
        member_role="member",
        union_openid=None,
        username="测试成员",
    )
    return GroupAtMessageCreateEvent.model_construct(
        id="qq-message-tsk274",
        content="/bind",
        timestamp="2026-09-08T12:00:00+08:00",
        mentions=[],
        attachments=None,
        message_scene=None,
        message_type=None,
        msg_idx=None,
        msg_elements=None,
        event_id="qq-message-tsk274",
        to_me=True,
        reply=None,
        author=author,
        group_id="group-openid-tsk274",
        group_openid="group-openid-tsk274",
    )


def test_numeric_qq_openid_is_not_treated_as_trusted_onebot_qq() -> None:
    """A numeric-shaped QQ OpenID is still an untrusted protocol identity."""
    event = _qq_group_at("123456789")

    assert event_support.get_event_user_id(event) is None


def test_non_numeric_qq_openid_remains_unavailable_to_user_ban() -> None:
    event = _qq_group_at("member-openid-tsk274")

    assert event_support.get_event_user_id(event) is None


def test_existing_generic_onebot_identity_path_is_preserved() -> None:
    class OneBotEvent:
        def get_user_id(self) -> str:
            return "10086"

    assert event_support.get_event_user_id(cast("Event", OneBotEvent())) == "10086"


@pytest.mark.asyncio
async def test_untrusted_qq_event_does_not_query_ban_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    class Service:
        async def is_user_banned(self, user_id: str, scope: str) -> bool:
            calls.append((user_id, scope))
            return True

    monkeypatch.setattr(event_support, "get_service", lambda: Service())
    bot = cast(
        "Bot",
        type("Bot", (), {"config": type("Config", (), {"superusers": set()})()})(),
    )

    result = await event_support.is_event_banned(
        bot,
        _qq_group_at("123456789"),
        "command",
    )

    assert result is False
    assert calls == []
