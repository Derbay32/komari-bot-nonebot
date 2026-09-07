"""TSK-273：OneBot 原生 reply 身份取证行为基线。

流程方向是 QQ 原始命令 → 官 Bot 引用挑战 → OneBot 读取原始命令：真实 OneBot
V11 事件提供两端消息与 reply 结构，``message_fetcher`` 提供可替换的完整
``get_msg`` 读取口。测试不调用正式角色绑定写入，也不把两端消息 ID、正文或
``to_me`` 当成身份替代。
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from contextlib import contextmanager, suppress
from dataclasses import FrozenInstanceError, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

import pytest
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment
from nonebot.adapters.onebot.v11.event import Reply, Sender

from komari_bot.plugins.character_binding.reply_evidence import (
    ReplyEvidence,
    ReplyEvidenceCollector,
    SessionCodeCollisionError,
)
from tests.group_admission.entry_gate_support import (
    ProbeBot,
    dispatch,
    event_gate_context,
)
from tests.group_admission.management_support import prepare_control_plane
from tests.group_admission.registry_isolation_support import (
    registry_isolation_context,
)
from tests.group_admission.runtime_support import AdmissionStorageFake, stored_policy

PACKAGE_NAME = "komari_bot.plugins.character_binding"
REPLY_EVIDENCE_MODULE = "komari_bot.plugins.character_binding.reply_evidence"
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
SECOND_APP_ID = "app-tsk273-secondary"
GROUP_OPENID = "group-openid-tsk273"
MEMBER_OPENID = "member-openid-tsk273"
OFFICIAL_BOT_QQ = "9001001"
SECOND_OFFICIAL_BOT_QQ = "9001002"
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


class BlockingMessageFetcher(MessageFetcher):
    """让 get_msg 在校验中途挂起，以验证会话失效后的二次检查。"""

    def __init__(self, payloads: Mapping[int, Mapping[str, object]]) -> None:
        super().__init__(payloads)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, message_id: int) -> Mapping[str, object]:
        self.calls.append(message_id)
        self.started.set()
        await self.release.wait()
        if message_id in self.fail_ids:
            raise RuntimeError("simulated get_msg failure")  # noqa: TRY003
        return self.payloads[message_id]


class PerMessageBlockingFetcher(MessageFetcher):
    """Block one target while allowing a second target to finish immediately."""

    def __init__(
        self,
        payloads: Mapping[int, Mapping[str, object]],
        blocked_message_id: int,
    ) -> None:
        super().__init__(payloads)
        self.blocked_message_id = blocked_message_id
        self.blocked_started = asyncio.Event()
        self.release_blocked = asyncio.Event()

    async def __call__(self, message_id: int) -> Mapping[str, object]:
        self.calls.append(message_id)
        if message_id == self.blocked_message_id:
            self.blocked_started.set()
            await self.release_blocked.wait()
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
) -> GroupMessageEvent:
    challenge_text = f"正在确认你的本群身份。会话码：{session_code}"
    original = Message(
        [MessageSegment.reply(quoted_message_id), MessageSegment.text(challenge_text)]
    )
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
        message=Message(challenge_text),
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


@pytest.mark.asyncio
async def test_accepts_configured_official_sender_when_to_me_is_false() -> None:
    """官方数字 QQ 与 OneBot self_id 不同，且 ``to_me`` 不是官方身份依据。"""
    code = "TSK273-OFFICIAL"
    original_id = 31001
    challenge_id = 41001
    original_text = COMMAND
    fetcher = MessageFetcher(
        {
            original_id: _get_msg_payload(
                message_id=original_id,
                original_text=original_text,
            )
        }
    )
    collector = _collector(fetcher=fetcher, clock=FrozenClock(BASE_TIME))
    _open(collector, code=code)

    original = _original_event(message_id=original_id, text=original_text)
    challenge = _challenge_event(
        message_id=challenge_id,
        session_code=code,
        quoted_message_id=original_id,
        quoted_text=original_text,
    )
    evidence = await _prime_and_capture(
        collector,
        original=original,
        challenge=challenge,
    )

    assert evidence is not None
    _assert_evidence(
        evidence,
        code=code,
        onebot_original_message_id=original_id,
        challenge_message_id=challenge_id,
    )
    assert challenge.to_me is False
    assert challenge.self_id == ONEBOT_SELF_ID
    assert fetcher.calls == [original_id]
    assert challenge_id != original_id


@pytest.mark.asyncio
async def test_interleaved_same_text_sessions_use_native_reply_identity() -> None:
    """同群多人同文案、挑战先后交错时按原生 reply 分别关联。"""
    code_one = "TSK273-ONE"
    code_two = "TSK273-TWO"
    original_one_id = 32001
    original_two_id = 32002
    challenge_one_id = 42001
    challenge_two_id = 42002
    fetcher = MessageFetcher(
        {
            original_one_id: _get_msg_payload(
                message_id=original_one_id,
                original_text=COMMAND,
                sender_id=21001,
            ),
            original_two_id: _get_msg_payload(
                message_id=original_two_id,
                original_text=COMMAND,
                sender_id=21002,
            ),
        }
    )
    collector = _collector(fetcher=fetcher, clock=FrozenClock(BASE_TIME))
    challenge_two = _challenge_event(
        message_id=challenge_two_id,
        session_code=code_two,
        quoted_message_id=original_two_id,
        quoted_text=COMMAND,
        quoted_sender=21002,
    )
    challenge_one = _challenge_event(
        message_id=challenge_one_id,
        session_code=code_one,
        quoted_message_id=original_one_id,
        quoted_text=COMMAND,
        quoted_sender=21001,
    )
    original_one = _original_event(message_id=original_one_id, member_qq=21001)
    original_two = _original_event(message_id=original_two_id, member_qq=21002)

    # OneBot sees both valid /bind originals before QQ creates the sessions;
    # the later challenges then arrive in the opposite order.
    results = [
        await collector.handle_event(original_two),
        await collector.handle_event(original_one),
    ]
    _open(
        collector,
        code=code_one,
        member_openid="member-one",
        qq_message_id="qq-one",
    )
    _open(
        collector,
        code=code_two,
        member_openid="member-two",
        qq_message_id="qq-two",
    )
    results.extend(
        [
            await collector.handle_event(challenge_two),
            await collector.handle_event(challenge_one),
        ]
    )

    assert results[0] is None
    assert results[1] is None
    assert results[2] is not None
    assert results[3] is not None
    _assert_evidence(
        results[2],
        code=code_two,
        member_qq=21002,
        member_openid="member-two",
        onebot_original_message_id=original_two_id,
        challenge_message_id=challenge_two_id,
        qq_message_id="qq-two",
    )
    _assert_evidence(
        results[3],
        code=code_one,
        member_qq=21001,
        member_openid="member-one",
        onebot_original_message_id=original_one_id,
        challenge_message_id=challenge_one_id,
        qq_message_id="qq-one",
    )
    assert fetcher.calls == [original_two_id, original_one_id]


@pytest.mark.asyncio
async def test_different_sessions_progress_while_one_get_msg_is_blocked() -> None:
    """一个成员的慢 get_msg 不得阻塞另一成员的独立会话。"""
    first_original_id = 32101
    second_original_id = 32102
    first_challenge_id = 42101
    second_challenge_id = 42102
    fetcher = PerMessageBlockingFetcher(
        {
            first_original_id: _get_msg_payload(
                message_id=first_original_id,
                original_text=COMMAND,
                sender_id=21001,
            ),
            second_original_id: _get_msg_payload(
                message_id=second_original_id,
                original_text=COMMAND,
                sender_id=21002,
            ),
        },
        blocked_message_id=first_original_id,
    )
    collector = _collector(fetcher=fetcher, clock=FrozenClock(BASE_TIME))
    await collector.handle_event(
        _original_event(message_id=first_original_id, member_qq=21001)
    )
    await collector.handle_event(
        _original_event(message_id=second_original_id, member_qq=21002)
    )
    _open(
        collector,
        code="TSK273-CONCURRENT-ONE",
        member_openid="member-one",
        qq_message_id="qq-one",
    )
    _open(
        collector,
        code="TSK273-CONCURRENT-TWO",
        member_openid="member-two",
        qq_message_id="qq-two",
    )
    first_task = asyncio.create_task(
        collector.handle_event(
            _challenge_event(
                message_id=first_challenge_id,
                session_code="TSK273-CONCURRENT-ONE",
                quoted_message_id=first_original_id,
                quoted_text=COMMAND,
                quoted_sender=21001,
            )
        )
    )
    await asyncio.wait_for(fetcher.blocked_started.wait(), timeout=1)
    second_task = asyncio.create_task(
        collector.handle_event(
            _challenge_event(
                message_id=second_challenge_id,
                session_code="TSK273-CONCURRENT-TWO",
                quoted_message_id=second_original_id,
                quoted_text=COMMAND,
                quoted_sender=21002,
            )
        )
    )

    try:
        second_evidence = await asyncio.wait_for(second_task, timeout=1)
        assert second_evidence is not None
        _assert_evidence(
            second_evidence,
            code="TSK273-CONCURRENT-TWO",
            member_qq=21002,
            member_openid="member-two",
            onebot_original_message_id=second_original_id,
            challenge_message_id=second_challenge_id,
            qq_message_id="qq-two",
        )
        assert not first_task.done()
    finally:
        fetcher.release_blocked.set()
        first_evidence = await first_task
        if not second_task.done():
            second_task.cancel()
            with suppress(asyncio.CancelledError):
                await second_task

    assert first_evidence is not None
    _assert_evidence(
        first_evidence,
        code="TSK273-CONCURRENT-ONE",
        member_qq=21001,
        member_openid="member-one",
        onebot_original_message_id=first_original_id,
        challenge_message_id=first_challenge_id,
        qq_message_id="qq-one",
    )
    assert fetcher.calls == [first_original_id, second_original_id]


@pytest.mark.asyncio
async def test_signed_message_id_and_independent_real_id_are_valid() -> None:
    """OneBot message_id 可带符号，real_id 是独立事实。"""
    code = "TSK273-SIGNED-ID"
    original_id = -38101
    challenge_id = 48101
    real_id = 712345
    fetcher = MessageFetcher(
        {
            original_id: _get_msg_payload(
                message_id=original_id,
                returned_real_id=real_id,
                original_text=COMMAND,
            )
        }
    )
    collector = _collector(fetcher=fetcher, clock=FrozenClock(BASE_TIME))
    _open(collector, code=code)

    evidence = await _prime_and_capture(
        collector,
        original=_original_event(message_id=original_id),
        challenge=_challenge_event(
            message_id=challenge_id,
            session_code=code,
            quoted_message_id=original_id,
            quoted_real_id=real_id,
            quoted_text=COMMAND,
        ),
    )

    assert evidence is not None
    _assert_evidence(
        evidence,
        code=code,
        onebot_original_message_id=original_id,
        challenge_message_id=challenge_id,
    )
    assert fetcher.calls == [original_id]


@pytest.mark.asyncio
async def test_duplicate_legal_challenge_is_idempotent() -> None:
    code = "TSK273-IDEMPOTENT"
    original_id = 33001
    challenge_id = 43001
    original_text = COMMAND
    fetcher = MessageFetcher(
        {
            original_id: _get_msg_payload(
                message_id=original_id,
                original_text=original_text,
            )
        }
    )
    collector = _collector(fetcher=fetcher, clock=FrozenClock(BASE_TIME))
    _open(collector, code=code)
    original = _original_event(message_id=original_id)
    challenge = _challenge_event(
        message_id=challenge_id,
        session_code=code,
        quoted_message_id=original_id,
        quoted_text=original_text,
    )

    first = await _prime_and_capture(
        collector,
        original=original,
        challenge=challenge,
    )
    second = await collector.handle_event(challenge)

    assert first is not None
    assert second == first
    assert fetcher.calls == [original_id]


@pytest.mark.asyncio
async def test_session_original_command_must_match_cached_original() -> None:
    code = "TSK273-COMMAND-MISMATCH"
    original_id = 33101
    challenge_id = 43101
    fetcher = MessageFetcher(
        {
            original_id: _get_msg_payload(
                message_id=original_id,
                original_text=COMMAND,
            )
        }
    )
    collector = _collector(fetcher=fetcher, clock=FrozenClock(BASE_TIME))
    _open(collector, code=code, original_command="/bind --qq-side-mismatch")

    await collector.handle_event(_original_event(message_id=original_id))
    challenge = _challenge_event(
        message_id=challenge_id,
        session_code=code,
        quoted_message_id=original_id,
        quoted_text=COMMAND,
    )

    assert await collector.handle_event(challenge) is None
    assert collector.get_evidence(code) is None


def test_open_session_returns_an_immutable_public_snapshot() -> None:
    code = "TSK273-IMMUTABLE-SESSION"
    collector = _collector(
        fetcher=MessageFetcher({}),
        clock=FrozenClock(BASE_TIME),
    )

    session = collector.open_session(
        session_code=code,
        group_openid=GROUP_OPENID,
        member_openid=MEMBER_OPENID,
        original_command=COMMAND,
        qq_message_id=QQ_ORIGINAL_MESSAGE_ID,
    )

    with pytest.raises(FrozenInstanceError):
        session.original_command = "/tampered"  # pyright: ignore[reportAttributeAccessIssue]
    current = collector.get_session(code)
    assert current is not None
    assert current is session
    assert current.original_command == COMMAND


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", ["outer_group", "reply_member"])
async def test_existing_evidence_rejects_late_outer_or_reply_conflict(
    conflict: str,
) -> None:
    code = f"TSK273-LATE-CONFLICT-{conflict}"
    original_id = 33201
    challenge_id = 43201
    fetcher = MessageFetcher(
        {
            original_id: _get_msg_payload(
                message_id=original_id,
                original_text=COMMAND,
            )
        }
    )
    collector = _collector(fetcher=fetcher, clock=FrozenClock(BASE_TIME))
    _open(collector, code=code)
    first = await _prime_and_capture(
        collector,
        original=_original_event(message_id=original_id),
        challenge=_challenge_event(
            message_id=challenge_id,
            session_code=code,
            quoted_message_id=original_id,
            quoted_text=COMMAND,
        ),
    )
    assert first is not None

    late = _challenge_event(
        message_id=challenge_id,
        session_code=code,
        quoted_message_id=original_id,
        quoted_text=COMMAND,
        group_id=GROUP_ID + 1 if conflict == "outer_group" else GROUP_ID,
        quoted_sender=MEMBER_QQ + 1 if conflict == "reply_member" else MEMBER_QQ,
    )

    assert await collector.handle_event(late) is None
    assert collector.get_evidence(code) == first
    assert fetcher.calls == [original_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", ["none", "sender", "segment"])
async def test_representative_malformed_get_msg_payload_fails_closed(
    malformed: str,
) -> None:
    code = f"TSK273-MALFORMED-{malformed}"
    original_id = 33301
    challenge_id = 43301
    valid_payload = _get_msg_payload(
        message_id=original_id,
        original_text=COMMAND,
    )
    if malformed == "none":
        payload: object = None
    else:
        payload = dict(valid_payload)
        if malformed == "sender":
            payload["sender"] = ["not-a-sender-mapping"]
        else:
            payload["message"] = [{"type": "at", "data": "not-a-mapping"}]
    fetcher = MessageFetcher(cast("Any", {original_id: payload}))
    collector = _collector(fetcher=fetcher, clock=FrozenClock(BASE_TIME))
    _open(collector, code=code)
    await collector.handle_event(_original_event(message_id=original_id))

    assert (
        await collector.handle_event(
            _challenge_event(
                message_id=challenge_id,
                session_code=code,
                quoted_message_id=original_id,
                quoted_text=COMMAND,
            )
        )
        is None
    )
    assert collector.get_evidence(code) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "event_kwargs", "payload_kwargs"),
    [
        ("wrong_official_sender", {"official_sender": "9001002"}, {}),
        ("private_original_message", {}, {"message_type": "private"}),
        ("cross_group_payload", {}, {"group_id": GROUP_ID + 1}),
        ("missing_group_id", {}, {"group_id": None}),
        ("wrong_original_sender", {}, {"sender_id": MEMBER_QQ + 99}),
        (
            "original_sender_is_official_bot",
            {},
            {"sender_id": int(OFFICIAL_BOT_QQ)},
        ),
        (
            "body_mismatch",
            {},
            {"message_text": "正文已被替换", "raw_message": "正文已被替换"},
        ),
        ("message_id_mismatch", {}, {"returned_message_id": 34099}),
    ],
)
async def test_invalid_sender_type_group_or_body_fails_closed(
    case: str,
    event_kwargs: dict[str, object],
    payload_kwargs: dict[str, object],
) -> None:
    del case
    code = "TSK273-INVALID"
    original_id = 34001
    challenge_id = 44001
    original_text = COMMAND
    fetcher = MessageFetcher(
        {
            original_id: _get_msg_payload(
                message_id=original_id,
                original_text=original_text,
                **cast("Any", payload_kwargs),
            )
        }
    )
    collector = _collector(fetcher=fetcher, clock=FrozenClock(BASE_TIME))
    _open(collector, code=code)
    original = _original_event(message_id=original_id)
    challenge = _challenge_event(
        message_id=challenge_id,
        session_code=code,
        quoted_message_id=original_id,
        quoted_text=original_text,
        **cast("Any", event_kwargs),
    )
    await collector.handle_event(original)

    assert await collector.handle_event(challenge) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "event_kwargs"),
    [
        ("cross_group", {"group_id": GROUP_ID + 1}),
        ("cross_reply_target", {"quoted_message_id": 55099}),
    ],
)
async def test_cross_scope_or_wrong_reply_target_never_produces_evidence(
    field: str,
    event_kwargs: dict[str, object],
) -> None:
    del field
    code = "TSK273-SCOPE"
    original_id = 35001
    challenge_id = 45001
    original_text = COMMAND
    fetcher = MessageFetcher(
        {
            original_id: _get_msg_payload(
                message_id=original_id,
                original_text=original_text,
            ),
            55099: _get_msg_payload(
                message_id=55099,
                original_text=original_text,
            ),
        }
    )
    collector = _collector(fetcher=fetcher, clock=FrozenClock(BASE_TIME))
    _open(collector, code=code)
    original = _original_event(message_id=original_id)
    challenge = _challenge_event(
        message_id=challenge_id,
        session_code=code,
        quoted_message_id=int(
            cast("int", event_kwargs.get("quoted_message_id", original_id))
        ),
        quoted_text=original_text,
        group_id=int(cast("int", event_kwargs.get("group_id", GROUP_ID))),
    )

    await collector.handle_event(original)
    assert await collector.handle_event(challenge) is None


@pytest.mark.asyncio
async def test_missing_native_reply_does_not_call_get_msg() -> None:
    code = "TSK273-NO-REPLY"
    fetcher = MessageFetcher({})
    collector = _collector(fetcher=fetcher, clock=FrozenClock(BASE_TIME))
    _open(collector, code=code)
    challenge_text = f"正在确认你的本群身份。会话码：{code}"
    event = _group_event(
        message_id=46001,
        user_id=int(OFFICIAL_BOT_QQ),
        text=challenge_text,
        self_id=ONEBOT_SELF_ID,
        to_me=False,
        message=Message(challenge_text),
        original_message=Message(challenge_text),
        sender_id=OFFICIAL_BOT_QQ,
    )

    assert await collector.handle_event(event) is None
    assert fetcher.calls == []


@pytest.mark.asyncio
async def test_get_msg_failure_is_silent_and_does_not_create_evidence() -> None:
    code = "TSK273-GET-MSG-FAIL"
    original_id = 37001
    challenge_id = 47001
    original_text = COMMAND
    fetcher = MessageFetcher(
        {
            original_id: _get_msg_payload(
                message_id=original_id,
                original_text=original_text,
            )
        }
    )
    fetcher.fail_ids.add(original_id)
    collector = _collector(fetcher=fetcher, clock=FrozenClock(BASE_TIME))
    _open(collector, code=code)
    await collector.handle_event(_original_event(message_id=original_id))
    challenge = _challenge_event(
        message_id=challenge_id,
        session_code=code,
        quoted_message_id=original_id,
        quoted_text=original_text,
    )

    assert await collector.handle_event(challenge) is None
    assert fetcher.calls == [original_id]


@pytest.mark.asyncio
async def test_unmentioned_bind_is_not_cached_as_original_evidence() -> None:
    """普通群聊即使正文为 /bind，也必须缺少官 Bot 原生 @ 才拒绝。"""
    code = "TSK273-NO-MENTION"
    original_id = 37501
    challenge_id = 47501
    fetcher = MessageFetcher(
        {
            original_id: _get_msg_payload(
                message_id=original_id,
                original_text=COMMAND,
                mention_official=False,
            )
        }
    )
    collector = _collector(fetcher=fetcher, clock=FrozenClock(BASE_TIME))
    _open(collector, code=code)
    await collector.handle_event(
        _original_event(
            message_id=original_id,
            text=COMMAND,
            mention_official=False,
        )
    )
    challenge = _challenge_event(
        message_id=challenge_id,
        session_code=code,
        quoted_message_id=original_id,
        quoted_text=COMMAND,
        quoted_has_mention=False,
    )

    assert await collector.handle_event(challenge) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidation", ["cancel", "reset", "expire"])
async def test_inflight_get_msg_cannot_resurrect_invalidated_session(
    invalidation: str,
) -> None:
    code = f"TSK273-INFLIGHT-{invalidation}"
    original_id = 37601
    challenge_id = 47601
    clock = FrozenClock(BASE_TIME)
    fetcher = BlockingMessageFetcher(
        {
            original_id: _get_msg_payload(
                message_id=original_id,
                original_text=COMMAND,
            )
        }
    )
    collector = _collector(fetcher=fetcher, clock=clock)
    _open(collector, code=code)
    await collector.handle_event(_original_event(message_id=original_id))
    challenge = _challenge_event(
        message_id=challenge_id,
        session_code=code,
        quoted_message_id=original_id,
        quoted_text=COMMAND,
    )

    pending = asyncio.create_task(collector.handle_event(challenge))
    await fetcher.started.wait()
    if invalidation == "cancel":
        collector.cancel_session(code)
    elif invalidation == "reset":
        collector.reset_connection()
    else:
        clock.advance(timedelta(minutes=10))
    fetcher.release.set()

    assert await pending is None


@pytest.mark.asyncio
async def test_inflight_get_msg_rejects_when_original_cache_expires_first() -> None:
    """原消息缓存可先于仍有效的会话到期，释放旧读取也不得成功。"""
    code = "TSK273-INFLIGHT-CACHE-EXPIRED"
    original_id = 37602
    challenge_id = 47602
    clock = FrozenClock(BASE_TIME)
    fetcher = BlockingMessageFetcher(
        {
            original_id: _get_msg_payload(
                message_id=original_id,
                original_text=COMMAND,
            )
        }
    )
    collector = _collector(fetcher=fetcher, clock=clock)
    await collector.handle_event(_original_event(message_id=original_id))
    clock.advance(timedelta(minutes=9))
    _open(collector, code=code)
    challenge = _challenge_event(
        message_id=challenge_id,
        session_code=code,
        quoted_message_id=original_id,
        quoted_text=COMMAND,
    )

    pending = asyncio.create_task(collector.handle_event(challenge))
    await asyncio.wait_for(fetcher.started.wait(), timeout=1)
    clock.advance(timedelta(minutes=1))
    assert collector.get_session(code) is not None
    fetcher.release.set()

    assert await pending is None
    assert collector.get_evidence(code) is None


@pytest.mark.asyncio
async def test_cache_eviction_does_not_revoke_still_valid_evidence() -> None:
    """正常清理原消息缓存不得撤销尚未到期的已验 evidence。"""
    code = "TSK273-CACHE-EVICTION-AFTER-EVIDENCE"
    original_id = 37603
    challenge_id = 47603
    clock = FrozenClock(BASE_TIME)
    fetcher = MessageFetcher(
        {
            original_id: _get_msg_payload(
                message_id=original_id,
                original_text=COMMAND,
            )
        }
    )
    collector = _collector(fetcher=fetcher, clock=clock)
    await collector.handle_event(_original_event(message_id=original_id))
    clock.advance(timedelta(minutes=9))
    _open(collector, code=code)
    evidence = await collector.handle_event(
        _challenge_event(
            message_id=challenge_id,
            session_code=code,
            quoted_message_id=original_id,
            quoted_text=COMMAND,
        )
    )
    assert evidence is not None

    clock.advance(timedelta(minutes=1, microseconds=1))
    assert collector.get_session(code) is not None
    assert collector.get_evidence(code) == evidence


@pytest.mark.asyncio
async def test_cached_original_message_must_match_complete_get_msg() -> None:
    code = "TSK273-CACHE-MATCH"
    original_id = 38001
    challenge_id = 48001
    fetched_text = "原始命令被替换"
    fetcher = MessageFetcher(
        {
            original_id: _get_msg_payload(
                message_id=original_id,
                original_text=fetched_text,
            )
        }
    )
    collector = _collector(fetcher=fetcher, clock=FrozenClock(BASE_TIME))
    _open(collector, code=code)
    await collector.handle_event(_original_event(message_id=original_id, text=COMMAND))
    challenge = _challenge_event(
        message_id=challenge_id,
        session_code=code,
        quoted_message_id=original_id,
        quoted_text=fetched_text,
    )

    assert await collector.handle_event(challenge) is None


@pytest.mark.asyncio
async def test_random_code_collision_does_not_overwrite_first_session() -> None:
    code = "TSK273-COLLISION"
    original_id = 39001
    challenge_id = 49001
    original_text = COMMAND
    fetcher = MessageFetcher(
        {
            original_id: _get_msg_payload(
                message_id=original_id,
                original_text=original_text,
            )
        }
    )
    collector = _collector(fetcher=fetcher, clock=FrozenClock(BASE_TIME))
    _open(collector, code=code, member_openid=MEMBER_OPENID)

    with pytest.raises(SessionCodeCollisionError):
        _open(collector, code=code, member_openid="member-other")

    evidence = await _prime_and_capture(
        collector,
        original=_original_event(message_id=original_id),
        challenge=_challenge_event(
            message_id=challenge_id,
            session_code=code,
            quoted_message_id=original_id,
            quoted_text=original_text,
        ),
    )
    assert evidence is not None
    _assert_evidence(
        evidence,
        code=code,
        onebot_original_message_id=original_id,
        challenge_message_id=challenge_id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("offset", "accepted"),
    [
        (timedelta(minutes=10) - timedelta(microseconds=1), True),
        (timedelta(minutes=10), False),
    ],
)
async def test_session_expiry_has_an_absolute_ten_minute_boundary(
    offset: timedelta,
    accepted: bool,  # noqa: FBT001
) -> None:
    code = f"TSK273-EXPIRY-{accepted}"
    original_id = 40001
    challenge_id = 50001
    original_text = COMMAND
    clock = FrozenClock(BASE_TIME)
    fetcher = MessageFetcher(
        {
            original_id: _get_msg_payload(
                message_id=original_id,
                original_text=original_text,
            )
        }
    )
    collector = _collector(fetcher=fetcher, clock=clock)
    _open(collector, code=code)
    clock.advance(offset)
    evidence = await _prime_and_capture(
        collector,
        original=_original_event(message_id=original_id),
        challenge=_challenge_event(
            message_id=challenge_id,
            session_code=code,
            quoted_message_id=original_id,
            quoted_text=original_text,
        ),
    )

    assert (evidence is not None) is accepted


@pytest.mark.asyncio
async def test_cancel_restart_and_reconnect_invalidate_old_evidence() -> None:
    old_code = "TSK273-OLD"
    new_code = "TSK273-NEW"
    old_original_id = 41001
    old_challenge_id = 51001
    new_original_id = 41002
    new_challenge_id = 51002
    old_text = COMMAND
    new_text = COMMAND
    fetcher = MessageFetcher(
        {
            old_original_id: _get_msg_payload(
                message_id=old_original_id,
                original_text=old_text,
            ),
            new_original_id: _get_msg_payload(
                message_id=new_original_id,
                original_text=new_text,
                sender_id=MEMBER_QQ + 1,
            ),
        }
    )
    clock = FrozenClock(BASE_TIME)
    collector = _collector(fetcher=fetcher, clock=clock)
    _open(collector, code=old_code)
    old_challenge = _challenge_event(
        message_id=old_challenge_id,
        session_code=old_code,
        quoted_message_id=old_original_id,
        quoted_text=old_text,
    )
    old_original = _original_event(message_id=old_original_id)
    await collector.handle_event(old_original)
    collector.cancel_session(old_code)
    assert await collector.handle_event(old_challenge) is None

    restarted = _collector(fetcher=fetcher, clock=clock)
    assert await restarted.handle_event(old_challenge) is None

    collector.reset_connection()
    assert await collector.handle_event(old_challenge) is None

    _open(
        collector,
        code=new_code,
        member_openid="member-new",
        original_command=new_text,
        qq_message_id="qq-new",
    )
    new_original = _original_event(
        message_id=new_original_id,
        member_qq=MEMBER_QQ + 1,
        text=new_text,
    )
    new_challenge = _challenge_event(
        message_id=new_challenge_id,
        session_code=new_code,
        quoted_message_id=new_original_id,
        quoted_text=new_text,
        quoted_sender=MEMBER_QQ + 1,
    )
    evidence = await _prime_and_capture(
        collector,
        original=new_original,
        challenge=new_challenge,
    )
    assert evidence is not None
    assert evidence.member_openid == "member-new"
    assert evidence.connection_generation == 1
    assert evidence.challenge_message_id == new_challenge_id
    assert evidence.onebot_original_message_id == new_original_id
    assert evidence.qq_message_id == "qq-new"
    assert evidence.original_command == new_text


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


def _registered_evidence_matchers() -> list[Any]:
    import nonebot.matcher as matcher_module

    return [
        matcher
        for matchers in matcher_module.matchers.values()
        for matcher in matchers
        if getattr(getattr(matcher, "_source", None), "module_name", None)
        == REPLY_EVIDENCE_MODULE
    ]


@contextmanager
def _runtime_collectors_context(
    module: Any,
    collectors: tuple[ReplyEvidenceCollector, ...],
) -> Iterator[None]:
    """只通过公开运行时注入口临时提供已配置 collectors。"""
    previous = module.get_runtime_collectors()
    module.set_runtime_collectors(collectors)
    try:
        yield
    finally:
        module.set_runtime_collectors(previous)


def test_real_character_binding_package_registers_nonblocking_evidence_matcher() -> None:
    """真实包导入必须注册监听器，不能由测试 shim 冒充。"""
    with registry_isolation_context(), _real_character_binding_package():
        matchers = _registered_evidence_matchers()
        assert len(matchers) == 1
        assert matchers[0].type == "message"
        assert matchers[0].block is False


def test_runtime_collectors_keep_multiple_app_configurations() -> None:
    """运行时注册表按 app 保留多个配置，不以单例覆盖。"""
    with registry_isolation_context(), _real_character_binding_package():
        reply_evidence = importlib.import_module(REPLY_EVIDENCE_MODULE)
        first = reply_evidence.ReplyEvidenceCollector(
            app_id=APP_ID,
            official_bot_qq=OFFICIAL_BOT_QQ,
            message_fetcher=MessageFetcher({}),
            clock=FrozenClock(BASE_TIME),
        )
        second = reply_evidence.ReplyEvidenceCollector(
            app_id=SECOND_APP_ID,
            official_bot_qq=SECOND_OFFICIAL_BOT_QQ,
            message_fetcher=MessageFetcher({}),
            clock=FrozenClock(BASE_TIME),
        )

        with _runtime_collectors_context(reply_evidence, (first, second)):
            assert reply_evidence.get_runtime_collectors() == (first, second)


@pytest.mark.asyncio
async def test_unconfigured_runtime_listener_silently_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """273 不生成配置；274 未注入 collector 时监听器静默跳过。"""
    storage = AdmissionStorageFake(
        stored_policy(1, {"mode": "blacklist", "group_ids": [GROUP_ID]})
    )
    await prepare_control_plane(monkeypatch, storage)

    async with event_gate_context():
        with _real_character_binding_package():
            reply_evidence = importlib.import_module(REPLY_EVIDENCE_MODULE)
            bot = ProbeBot()
            with _runtime_collectors_context(reply_evidence, ()):
                await dispatch(
                    bot,
                    _challenge_event(
                        message_id=61001,
                        session_code="TSK273-NO-RUNTIME",
                        quoted_message_id=61002,
                        quoted_text=COMMAND,
                        group_id=GROUP_ID + 1,
                    ),
                )
                assert reply_evidence.get_runtime_collectors() == ()

    assert bot.calls == []


@pytest.mark.asyncio
async def test_admitted_event_reaches_explicitly_injected_collector_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """准入通过后只调用显式注入的已配置实例，拒绝群不进入 collector。"""
    storage = AdmissionStorageFake(
        stored_policy(1, {"mode": "blacklist", "group_ids": [GROUP_ID]})
    )
    await prepare_control_plane(monkeypatch, storage)
    calls: list[GroupMessageEvent] = []

    async with event_gate_context():
        with _real_character_binding_package():
            reply_evidence = importlib.import_module(REPLY_EVIDENCE_MODULE)
            collector = reply_evidence.ReplyEvidenceCollector(
                app_id=APP_ID,
                official_bot_qq=OFFICIAL_BOT_QQ,
                message_fetcher=MessageFetcher({}),
                clock=FrozenClock(BASE_TIME),
            )

            async def _spy(event: object) -> None:
                assert isinstance(event, GroupMessageEvent)
                calls.append(event)

            monkeypatch.setattr(collector, "handle_event", _spy)
            bot = ProbeBot()
            with _runtime_collectors_context(reply_evidence, (collector,)):
                admitted = _challenge_event(
                    message_id=62001,
                    session_code="TSK273-LISTENER",
                    quoted_message_id=62002,
                    quoted_text=COMMAND,
                    group_id=GROUP_ID + 1,
                )
                rejected = _challenge_event(
                    message_id=62003,
                    session_code="TSK273-LISTENER",
                    quoted_message_id=62004,
                    quoted_text=COMMAND,
                    group_id=GROUP_ID,
                )
                await dispatch(bot, admitted)
                await dispatch(bot, rejected)

    assert calls == [admitted]
    assert bot.calls == []
