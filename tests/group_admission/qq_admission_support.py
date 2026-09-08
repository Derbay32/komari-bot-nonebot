"""TSK-274 QQ protocol fakes and public-contract helpers.

The helpers construct real NoneBot QQ event models without opening a driver or
calling a QQ endpoint.  Production qualification is always reached through
the public ``group_admission`` package surface.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any, cast

import nonebot
import nonebot.adapters
import nonebot.message as message_module
from nonebot.adapters.qq import Bot as QQBot
from nonebot.adapters.qq.adapter import Adapter as QQAdapter
from nonebot.adapters.qq.config import BotInfo, Intents
from nonebot.adapters.qq.event import (
    C2CMessageCreateEvent,
    DirectMessageCreateEvent,
    GroupAtMessageCreateEvent,
    GroupMessageCreateEvent,
    InteractionCreateEvent,
    MessageCreateEvent,
)
from nonebot.adapters.qq.models.guild import User
from nonebot.adapters.qq.models.qq import FriendAuthor, GroupMemberAuthor
from nonebot.typing import T_State  # noqa: TC002

from tests.group_admission.entry_gate_support import event_gate_context
from tests.group_admission.registry_isolation_support import (
    registry_isolation_context,
)

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

APP_ID = "app-tsk274"
SECOND_APP_ID = "app-tsk274-2"
GROUP_OPENID = "group-openid-tsk274"
SECOND_GROUP_OPENID = "group-openid-tsk274-2"
MEMBER_OPENID = "member-openid-tsk274"
SECOND_MEMBER_OPENID = "member-openid-tsk274-2"
QQ_MESSAGE_ID = "qq-message-tsk274"
GROUP_ID = 274001
MEMBER_QQ = 274002
OFFICIAL_BOT_QQ = 9274001


def _qq_bot_info(app_id: str) -> BotInfo:
    return BotInfo(
        id=app_id,
        token="test-token",
        secret="test-secret",
        intent=Intents(c2c_group_at_messages=True),
        use_websocket=False,
    )


class QQProbeBot(QQBot):
    """Real QQ Bot identity with a network-free ``call_api`` override."""

    def __init__(self, self_id: str = APP_ID) -> None:
        adapter = cast("QQAdapter", QQAdapter.__new__(QQAdapter))
        super().__init__(adapter, self_id, _qq_bot_info(self_id))
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call_api(self, api: str, **data: object) -> Any:
        self.calls.append((api, data))
        return None


def _group_author(member_openid: str = MEMBER_OPENID) -> GroupMemberAuthor:
    return GroupMemberAuthor.model_construct(
        id="qq-member-id",
        bot=False,
        member_openid=member_openid,
        member_role="member",
        union_openid=None,
        username="测试成员",
    )


def _friend_author(member_openid: str = MEMBER_OPENID) -> FriendAuthor:
    return FriendAuthor.model_construct(
        id="qq-friend-id",
        user_openid=member_openid,
        union_openid=None,
        username="测试成员",
    )


def _guild_author(member_openid: str = MEMBER_OPENID) -> User:
    return User.model_construct(
        id=member_openid,
        username="测试成员",
        avatar=None,
        bot=False,
        union_openid=None,
        union_user_account=None,
    )


def make_group_at(
    *,
    content: str = "/bind",
    group_openid: str = GROUP_OPENID,
    member_openid: str = MEMBER_OPENID,
    message_id: str = QQ_MESSAGE_ID,
    event_cls: type[GroupAtMessageCreateEvent] = GroupAtMessageCreateEvent,
) -> GroupAtMessageCreateEvent:
    """Construct a native QQ group-at event, including no textual @ marker."""
    return cast(
        "Any",
        event_cls,
    ).model_construct(
        id=message_id,
        content=content,
        timestamp="2026-09-08T12:00:00+08:00",
        mentions=[],
        attachments=None,
        message_scene=None,
        message_type=None,
        msg_idx=None,
        msg_elements=None,
        event_id=message_id,
        to_me=True,
        reply=None,
        author=_group_author(member_openid),
        group_id=group_openid,
        group_openid=group_openid,
    )


def make_plain_group(
    *,
    content: str = "/bind",
    group_openid: str = GROUP_OPENID,
    member_openid: str = MEMBER_OPENID,
    message_id: str = QQ_MESSAGE_ID,
) -> GroupMessageCreateEvent:
    return GroupMessageCreateEvent.model_construct(
        id=message_id,
        content=content,
        timestamp="2026-09-08T12:00:00+08:00",
        mentions=[],
        attachments=None,
        message_scene=None,
        message_type=None,
        msg_idx=None,
        msg_elements=None,
        event_id=message_id,
        to_me=False,
        reply=None,
        author=_group_author(member_openid),
        group_id=group_openid,
        group_openid=group_openid,
    )


def make_c2c(*, content: str = "/bind") -> C2CMessageCreateEvent:
    return C2CMessageCreateEvent.model_construct(
        id=QQ_MESSAGE_ID,
        content=content,
        timestamp="2026-09-08T12:00:00+08:00",
        mentions=[],
        attachments=None,
        message_scene=None,
        message_type=None,
        msg_idx=None,
        msg_elements=None,
        event_id=QQ_MESSAGE_ID,
        to_me=True,
        reply=None,
        author=_friend_author(),
    )


def make_guild_message(*, content: str = "/bind") -> MessageCreateEvent:
    return MessageCreateEvent.model_construct(
        id=QQ_MESSAGE_ID,
        channel_id="channel-tsk274",
        guild_id="guild-tsk274",
        content=content,
        timestamp="2026-09-08T12:00:00+08:00",
        edited_timestamp=None,
        mention_everyone=False,
        author=_guild_author(),
        attachments=[],
        embeds=[],
        mentions=[],
        member=None,
        ark=None,
        seq=None,
        seq_in_channel=None,
        message_reference=None,
        src_guild_id=None,
        event_id=QQ_MESSAGE_ID,
        to_me=False,
        reply=None,
    )


def make_direct_message(*, content: str = "/bind") -> DirectMessageCreateEvent:
    return DirectMessageCreateEvent.model_construct(
        id=QQ_MESSAGE_ID,
        channel_id="channel-tsk274",
        guild_id="guild-tsk274",
        content=content,
        timestamp="2026-09-08T12:00:00+08:00",
        edited_timestamp=None,
        mention_everyone=False,
        author=_guild_author(),
        attachments=[],
        embeds=[],
        mentions=[],
        member=None,
        ark=None,
        seq=None,
        seq_in_channel=None,
        message_reference=None,
        src_guild_id=None,
        event_id=QQ_MESSAGE_ID,
        to_me=True,
        reply=None,
    )


def make_interaction() -> InteractionCreateEvent:
    return InteractionCreateEvent.model_construct(
        id="interaction-tsk274",
        type=12,
        version=1,
        timestamp="2026-09-08T12:00:00+08:00",
        scene="group",
        chat_type=2,
        guild_id=None,
        channel_id=None,
        user_openid=MEMBER_OPENID,
        group_openid=GROUP_OPENID,
        group_member_openid=MEMBER_OPENID,
        application_id=APP_ID,
        data=object(),
        event_id="interaction-tsk274",
    )


class ForgedGroupAtMessageCreateEvent(GroupAtMessageCreateEvent):
    """A subclass must not enter the exact QQ event allow-list."""


def admission_package() -> Any:
    """Load the real package and fail clearly while the production seam is red."""
    return importlib.import_module("komari_bot.plugins.group_admission")


def require_qq_contract(*names: str) -> Any:
    package = admission_package()
    missing = [name for name in names if not hasattr(package, name)]
    assert not missing, f"TSK-274 public QQ contract missing: {missing}"
    return package


def register_state_probe(captured: list[dict[object, object]]) -> object:
    """Register one generic matcher that records the exact shared state dict."""
    matcher = nonebot.on_message(priority=1, block=False)

    @matcher.handle()
    async def _handle(state: T_State) -> None:
        captured.append(dict(state))

    return matcher


async def dispatch_qq(bot: QQProbeBot, event: object) -> None:
    await message_module.handle_event(
        cast("nonebot.adapters.Bot", bot),
        cast("nonebot.adapters.Event", event),
    )


def preprocessor_functions() -> list[object]:
    return list(message_module._event_preprocessors)


def public_state_token(state: dict[object, object]) -> Any:
    package = admission_package()
    getter = package.get_qq_admission_token
    return getter(state)


def qq_registry_context() -> AbstractContextManager[None]:
    """Document the two real registry contexts used by QQ gate tests."""
    return registry_isolation_context()


__all__ = [
    "APP_ID",
    "GROUP_ID",
    "GROUP_OPENID",
    "MEMBER_OPENID",
    "MEMBER_QQ",
    "OFFICIAL_BOT_QQ",
    "QQ_MESSAGE_ID",
    "ForgedGroupAtMessageCreateEvent",
    "QQProbeBot",
    "admission_package",
    "dispatch_qq",
    "event_gate_context",
    "make_c2c",
    "make_direct_message",
    "make_group_at",
    "make_guild_message",
    "make_interaction",
    "make_plain_group",
    "preprocessor_functions",
    "public_state_token",
    "qq_registry_context",
    "register_state_probe",
    "require_qq_contract",
]
