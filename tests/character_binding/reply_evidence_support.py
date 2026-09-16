"""TSK-273 OneBot 原生 reply 身份取证的共享测试辅助。

承载 ``test_reply_evidence`` 中被其他测试模块复用的事件/载荷构造器、
collector 工厂与真实包上下文管理器；行为基线测试本体留在
``test_reply_evidence.py``。
"""

from __future__ import annotations

import importlib
import sys
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment
from nonebot.adapters.onebot.v11.event import Reply, Sender

from komari_bot.plugins.character_binding.reply_evidence import (
    ReplyEvidence,
    ReplyEvidenceCollector,
)

PACKAGE_NAME = "komari_bot.plugins.character_binding"
PACKAGE_PATH = (
    Path(__file__).resolve().parents[2]
    / "komari_bot"
    / "plugins"
    / "character_binding"
)
# NoneBot test startup may already import these ORM-backed modules while
# registering the shared SQLModel metadata.  Reusing those module objects keeps
# a real package reload from defining the same tables twice.  Registration
# bearing modules remain reloadable below so this helper still observes the
# production commands and evidence listener.
_PRESERVE_REAL_PACKAGE_MODULES = frozenset(
    {
        f"{PACKAGE_NAME}.orm_models",
        f"{PACKAGE_NAME}.database",
        f"{PACKAGE_NAME}.manager",
    }
)

APP_ID = "app-tsk273"
GROUP_OPENID = "group-openid-tsk273"
MEMBER_OPENID = "member-openid-tsk273"
OFFICIAL_BOT_QQ = "9001001"
ONEBOT_SELF_ID = 7777001
GROUP_ID = 10001
MEMBER_QQ = 20001
COMMAND = "/bind"
QQ_ORIGINAL_MESSAGE_ID = "qq-msg-1001"
BASE_TIME = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


@dataclass
class FrozenClock:
    """可控 UTC 时钟，避免边界用例真实等待。"""

    current: datetime

    def __call__(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


class MessageFetcher:
    """记录 get_msg 读取并支持按消息注入失败。"""

    def __init__(self, payloads: Mapping[int, Mapping[str, object]]) -> None:
        self.payloads = dict(payloads)
        self.calls: list[int] = []
        self.fail_ids: set[int] = set()

    async def __call__(self, message_id: int) -> Mapping[str, object]:
        self.calls.append(message_id)
        if message_id in self.fail_ids:
            raise RuntimeError("simulated get_msg failure")  # noqa: TRY003
        return self.payloads[message_id]


def _sender(user_id: int | str, nickname: str) -> Sender:
    return Sender.model_construct(user_id=int(user_id), nickname=nickname)


def _bind_message(text: str = COMMAND) -> Message:
    return Message(
        [
            MessageSegment.at(OFFICIAL_BOT_QQ),
            MessageSegment.text(text),
        ]
    )


def _group_event(
    *,
    message_id: int,
    user_id: int | str,
    group_id: int = GROUP_ID,
    text: str = COMMAND,
    self_id: int = ONEBOT_SELF_ID,
    to_me: bool = False,
    message: Message | None = None,
    original_message: Message | None = None,
    reply: Reply | None = None,
    raw_message: str | None = None,
    sender_id: int | str | None = None,
) -> GroupMessageEvent:
    current_message = message or Message(text)
    source_message = original_message or current_message
    resolved_sender = user_id if sender_id is None else sender_id
    return GroupMessageEvent.model_construct(
        time=int(BASE_TIME.timestamp()),
        self_id=self_id,
        post_type="message",
        sub_type="normal",
        user_id=int(user_id),
        message_type="group",
        message_id=message_id,
        message=current_message,
        original_message=source_message,
        raw_message=text if raw_message is None else raw_message,
        font=14,
        sender=_sender(resolved_sender, "sender"),
        to_me=to_me,
        reply=reply,
        group_id=group_id,  # type: ignore[call-arg]
        anonymous=None,
    )


def _original_event(
    *,
    message_id: int,
    group_id: int = GROUP_ID,
    member_qq: int = MEMBER_QQ,
    text: str = COMMAND,
    mention_official: bool = True,
) -> GroupMessageEvent:
    message = _bind_message(text) if mention_official else Message(text)
    return _group_event(
        message_id=message_id,
        user_id=member_qq,
        group_id=group_id,
        text=text,
        message=message,
        original_message=message,
        raw_message=str(message),
        sender_id=member_qq,
    )


def _challenge_event(
    *,
    message_id: int,
    session_code: str,
    quoted_message_id: int,
    quoted_text: str,
    group_id: int = GROUP_ID,
    official_sender: str = OFFICIAL_BOT_QQ,
    quoted_sender: int = MEMBER_QQ,
    quoted_real_id: int | None = None,
    quoted_has_mention: bool = True,
    to_me: bool = False,
    body_format: Literal["text", "markdown"] = "markdown",
    challenge_body: str | None = None,
) -> GroupMessageEvent:
    challenge_text = (
        f"正在确认你的本群身份。会话码：{session_code}"
        if challenge_body is None
        else challenge_body
    )
    body_segment = (
        MessageSegment.text(challenge_text)
        if body_format == "text"
        else MessageSegment("markdown", {"content": challenge_text})
    )
    original = Message([MessageSegment.reply(quoted_message_id), body_segment])
    quoted_message = (
        _bind_message(quoted_text)
        if quoted_has_mention
        else Message(quoted_text)
    )
    reply = Reply.model_construct(
        time=int(BASE_TIME.timestamp()),
        message_type="group",
        message_id=quoted_message_id,
        real_id=(
            quoted_message_id
            if quoted_real_id is None
            else quoted_real_id
        ),
        sender=_sender(quoted_sender, "member"),
        message=quoted_message,
        group_id=group_id,
    )
    return _group_event(
        message_id=message_id,
        user_id=int(official_sender),
        group_id=group_id,
        text=challenge_text,
        self_id=ONEBOT_SELF_ID,
        to_me=to_me,
        message=Message([body_segment]),
        original_message=original,
        reply=reply,
        sender_id=official_sender,
    )


def _get_msg_payload(
    *,
    message_id: int,
    original_text: str,
    group_id: int | None = GROUP_ID,
    message_type: str = "group",
    sender_id: int | str = MEMBER_QQ,
    mention_official: bool = True,
    returned_message_id: int | None = None,
    returned_real_id: int | None = None,
    raw_message: str | None = None,
    message_text: str | None = None,
) -> dict[str, object]:
    resolved_text = message_text or original_text
    payload_message = _bind_message(resolved_text) if mention_official else Message(resolved_text)
    payload: dict[str, object] = {
        "time": int(BASE_TIME.timestamp()),
        "message_id": message_id if returned_message_id is None else returned_message_id,
        "real_id": message_id if returned_real_id is None else returned_real_id,
        "message_type": message_type,
        "sender": {"user_id": int(sender_id), "nickname": "member"},
        "message": [
            {"type": segment.type, "data": dict(segment.data)}
            for segment in payload_message
        ],
        "raw_message": (
            str(payload_message)
            if raw_message is None
            else raw_message
        ),
    }
    if group_id is not None:
        payload["group_id"] = group_id
    return payload


def _collector(
    *,
    fetcher: MessageFetcher,
    clock: FrozenClock,
    app_id: str = APP_ID,
    official_bot_qq: str = OFFICIAL_BOT_QQ,
) -> ReplyEvidenceCollector:
    return ReplyEvidenceCollector(
        app_id=app_id,
        official_bot_qq=official_bot_qq,
        message_fetcher=fetcher,
        clock=clock,
    )


def _open(
    collector: ReplyEvidenceCollector,
    *,
    code: str,
    group_openid: str = GROUP_OPENID,
    member_openid: str = MEMBER_OPENID,
    original_command: str = COMMAND,
    qq_message_id: str = QQ_ORIGINAL_MESSAGE_ID,
) -> None:
    collector.open_session(
        session_code=code,
        group_openid=group_openid,
        member_openid=member_openid,
        original_command=original_command,
        qq_message_id=qq_message_id,
    )


def _assert_evidence(
    evidence: ReplyEvidence,
    *,
    code: str,
    group_id: int = GROUP_ID,
    member_qq: int = MEMBER_QQ,
    member_openid: str = MEMBER_OPENID,
    group_openid: str = GROUP_OPENID,
    onebot_original_message_id: int,
    challenge_message_id: int,
    qq_message_id: str = QQ_ORIGINAL_MESSAGE_ID,
    original_command: str = COMMAND,
    generation: int = 0,
) -> None:
    assert evidence.app_id == APP_ID
    assert evidence.session_code == code
    assert evidence.group_openid == group_openid
    assert evidence.member_openid == member_openid
    assert evidence.group_id == str(group_id)
    assert evidence.member_qq == str(member_qq)
    assert evidence.original_command == original_command
    assert evidence.qq_message_id == qq_message_id
    assert evidence.onebot_original_message_id == onebot_original_message_id
    assert evidence.challenge_message_id == challenge_message_id
    assert evidence.connection_generation == generation
    assert evidence.qq_message_id != evidence.onebot_original_message_id
    assert evidence.challenge_message_id != evidence.onebot_original_message_id


async def _prime_and_capture(
    collector: ReplyEvidenceCollector,
    *,
    original: GroupMessageEvent,
    challenge: GroupMessageEvent,
) -> ReplyEvidence | None:
    await collector.handle_event(original)
    return await collector.handle_event(challenge)


@contextmanager
def _real_character_binding_package() -> Iterator[object]:
    """临时移除 tests/conftest 的 package shim，观察真实包注册面。"""
    saved_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == PACKAGE_NAME or name.startswith(f"{PACKAGE_NAME}.")
    }
    for name in saved_modules:
        if name in _PRESERVE_REAL_PACKAGE_MODULES:
            continue
        sys.modules.pop(name, None)

    parent = sys.modules.get("komari_bot.plugins")
    sentinel = object()
    previous_attr = getattr(parent, "character_binding", sentinel)
    if parent is not None and previous_attr is not sentinel:
        delattr(parent, "character_binding")

    try:
        package = importlib.import_module(PACKAGE_NAME)
        assert Path(str(getattr(package, "__file__", ""))).resolve() == (
            PACKAGE_PATH / "__init__.py"
        ).resolve()
        yield package
    finally:
        for name in list(sys.modules):
            if name == PACKAGE_NAME or name.startswith(f"{PACKAGE_NAME}."):
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)
        if parent is not None:
            if previous_attr is sentinel:
                with suppress(AttributeError):
                    delattr(parent, "character_binding")
            else:
                setattr(parent, "character_binding", previous_attr)  # noqa: B010
