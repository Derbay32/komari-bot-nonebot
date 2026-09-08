"""OneBot native-reply evidence collection for the character binding flow.

The QQ side of the binding flow owns the canonical application, group and
member identifiers.  This module only keeps a short-lived bridge to the
numeric identifiers observed by OneBot.  It deliberately has no persistence
or sending side effects.
"""

from __future__ import annotations

import asyncio
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Final

from nonebot import on_message

# NoneBot reflects these annotations at runtime to inject matcher dependencies.
from nonebot.adapters import Bot, Event  # noqa: TC002
from nonebot.adapters.onebot.v11 import (
    Bot as OneBotBot,
)
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageSegment,
)
from nonebot.adapters.onebot.v11.event import Reply

SESSION_TTL: Final[timedelta] = timedelta(seconds=600)
"""Absolute lifetime of a pending bridge session and its evidence."""

_SESSION_CODE_RE: Final[re.Pattern[str]] = re.compile(r"会话码：([^\s]+)")
_MAX_SESSIONS: Final[int] = 512
_MAX_ORIGINAL_MESSAGES: Final[int] = 1024
_MAX_PENDING_CHALLENGES: Final[int] = 1024

MessageFetcher = Callable[[int], Awaitable[Mapping[str, object]]]
Clock = Callable[[], datetime]
EvidenceReceiver = Callable[["ReplyEvidence"], Awaitable[object | None]]


class SessionCodeCollisionError(ValueError):
    """Raised when a session code would overwrite an existing session."""


@dataclass(frozen=True, slots=True)
class ReplyEvidence:
    """Verified bridge evidence for one QQ binding session."""

    app_id: str
    session_code: str
    group_openid: str
    member_openid: str
    group_id: str
    member_qq: str
    original_command: str
    qq_message_id: str
    onebot_original_message_id: int
    challenge_message_id: int
    connection_generation: int

    @property
    def onebot_group_id(self) -> str:
        """Return the numeric group identifier under an explicit name."""
        return self.group_id

    @property
    def onebot_member_qq(self) -> str:
        """Return the numeric member identifier under an explicit name."""
        return self.member_qq


@dataclass(frozen=True, slots=True)
class ReplyEvidenceSession:
    """Short-lived public state that a later binding step can consume."""

    app_id: str
    session_code: str
    group_openid: str
    member_openid: str
    original_command: str
    qq_message_id: str
    created_at: datetime
    expires_at: datetime
    connection_generation: int
    evidence: ReplyEvidence | None = None


@dataclass(frozen=True, slots=True)
class _OriginalMessage:
    message_id: int
    group_id: str
    member_qq: str
    command: str
    raw_message: str
    signature: tuple[tuple[str, tuple[tuple[str, str], ...]], ...]
    observed_at: datetime
    generation: int


def _canonical_id(value: object) -> str | None:
    """Return a positive numeric identifier in canonical string form."""
    if isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text.isdigit() or int(text) <= 0:
        return None
    return str(int(text))


def _message_signature(
    message: Message,
) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    """Build a deterministic signature for every message segment."""
    segments: list[tuple[str, tuple[tuple[str, str], ...]]] = []
    for segment in message:
        items = tuple(
            sorted((str(key), str(value)) for key, value in segment.data.items())
        )
        segments.append((segment.type, items))
    return tuple(segments)


def _message_from_payload(value: object) -> Message | None:
    """Convert a OneBot message array into the adapter's message object."""
    if isinstance(value, Message):
        return value
    if not isinstance(value, (list, tuple)):
        return None
    segments: list[MessageSegment] = []
    for raw_segment in value:
        if not isinstance(raw_segment, Mapping):
            return None
        segment_type = raw_segment.get("type")
        data = raw_segment.get("data")
        if not isinstance(segment_type, str) or not isinstance(data, Mapping):
            return None
        segments.append(MessageSegment(segment_type, dict(data)))
    return Message(segments)


def _native_reply_id(message: Message) -> int | None:
    """Return the single native reply target, if the message has one."""
    reply_ids: list[int] = []
    for segment in message:
        if segment.type != "reply":
            continue
        reply_id = _message_id(segment.data.get("id"))
        if reply_id is None:
            return None
        reply_ids.append(reply_id)
    if len(reply_ids) != 1:
        return None
    return reply_ids[0]


def _has_official_mention(message: Message, official_bot_qq: str) -> bool:
    return any(
        segment.type == "at"
        and _canonical_id(segment.data.get("qq")) == official_bot_qq
        for segment in message
    )


def _is_bind_command(message: Message, official_bot_qq: str) -> str | None:
    """Accept only a native mention followed by the exact ``/bind`` command."""
    if any(segment.type == "reply" for segment in message):
        return None
    if not _has_official_mention(message, official_bot_qq):
        return None
    command = message.extract_plain_text().strip()
    return command if command == "/bind" else None


def _event_group_id(event: GroupMessageEvent) -> str | None:
    return _canonical_id(getattr(event, "group_id", None))


def _event_member_id(event: GroupMessageEvent) -> str | None:
    event_user_id = _canonical_id(getattr(event, "user_id", None))
    sender = getattr(event, "sender", None)
    sender_user_id = _canonical_id(getattr(sender, "user_id", None))
    if event_user_id is None or sender_user_id is None or event_user_id != sender_user_id:
        return None
    return event_user_id


def _message_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text and (text.isdigit() or (text[0] == "-" and text[1:].isdigit())):
            return int(text)
    return None


def _message_text(message: Message) -> str:
    return message.extract_plain_text().strip()


def _challenge_code(event: GroupMessageEvent) -> str | None:
    candidates: list[str] = []
    for candidate in (
        getattr(event, "message", None),
        getattr(event, "original_message", None),
    ):
        if not isinstance(candidate, Message):
            continue
        candidates.extend(match.group(1) for match in _SESSION_CODE_RE.finditer(_message_text(candidate)))
    if not candidates or len(set(candidates)) != 1:
        return None
    return candidates[0]


@dataclass(frozen=True, slots=True)
class _Challenge:
    session_code: str
    target_message_id: int
    challenge_message_id: int
    group_id: str
    reply_message_type: str | None
    reply_member_qq: str | None
    reply_group_id: str | None
    reply_signature: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] | None
    observed_at: datetime
    generation: int


class ReplyEvidenceCollector:
    """Collect and verify OneBot evidence for configured QQ sessions."""

    def __init__(
        self,
        app_id: str,
        official_bot_qq: str,
        message_fetcher: MessageFetcher,
        clock: Clock,
    ) -> None:
        self.app_id = str(app_id)
        canonical_official_bot_qq = _canonical_id(official_bot_qq)
        if not self.app_id or canonical_official_bot_qq is None:
            raise ValueError("app_id and official_bot_qq are required")  # noqa: TRY003
        self.official_bot_qq: str = canonical_official_bot_qq
        self._message_fetcher = message_fetcher
        self._clock = clock
        self._connection_generation = 0
        self._sessions: OrderedDict[str, ReplyEvidenceSession] = OrderedDict()
        self._original_messages: OrderedDict[int, _OriginalMessage] = OrderedDict()
        self._pending_challenges: OrderedDict[tuple[str, int, int], _Challenge] = OrderedDict()
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._pending_tasks: set[asyncio.Task[None]] = set()

    @property
    def connection_generation(self) -> int:
        return self._connection_generation

    def open_session(
        self,
        session_code: str,
        group_openid: str,
        member_openid: str,
        original_command: str,
        qq_message_id: str,
    ) -> ReplyEvidenceSession:
        """Register one QQ-side session without requiring numeric IDs."""
        code = str(session_code).strip()
        if not code or any(char.isspace() for char in code) or len(code) > 128:
            raise ValueError("invalid session code")  # noqa: TRY003
        if not str(group_openid) or not str(member_openid):
            raise ValueError(  # noqa: TRY003
                "canonical group and member identifiers are required"
            )
        command = str(original_command).strip()
        if not command:
            raise ValueError("original command is required")  # noqa: TRY003
        qq_id = str(qq_message_id).strip()
        if not qq_id:
            raise ValueError("QQ message ID is required")  # noqa: TRY003

        now = self._now()
        self._prune(now)
        if code in self._sessions:
            raise SessionCodeCollisionError(code)
        session = ReplyEvidenceSession(
            app_id=self.app_id,
            session_code=code,
            group_openid=str(group_openid),
            member_openid=str(member_openid),
            original_command=command,
            qq_message_id=qq_id,
            created_at=now,
            expires_at=now + SESSION_TTL,
            connection_generation=self._connection_generation,
        )
        self._sessions[code] = session
        self._trim(self._sessions, _MAX_SESSIONS)
        self._schedule_pending_resolution(code)
        return session

    def cancel_session(self, session_code: str) -> None:
        """Invalidate a pending session and all challenges for its code."""
        code = str(session_code)
        self._sessions.pop(code, None)
        for key in tuple(self._pending_challenges):
            if key[0] == code:
                self._pending_challenges.pop(key, None)

    def reset_connection(self) -> None:
        """Advance the connection generation and invalidate all old evidence."""
        self._connection_generation += 1
        self._sessions.clear()
        self._original_messages.clear()
        self._pending_challenges.clear()

    def get_session(self, session_code: str) -> ReplyEvidenceSession | None:
        """Return current session state for a later binding workflow."""
        self._prune(self._now())
        return self._sessions.get(str(session_code))

    def get_evidence(self, session_code: str) -> ReplyEvidence | None:
        """Return evidence already accepted for a current session."""
        session = self.get_session(session_code)
        return session.evidence if session is not None else None

    def _schedule_pending_resolution(self, session_code: str) -> None:
        """Resume a challenge that arrived before its QQ session opened."""
        if not any(
            challenge.session_code == session_code
            and challenge.target_message_id in self._original_messages
            for challenge in self._pending_challenges.values()
        ):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._resolve_pending_for_session(session_code))
        self._pending_tasks.add(task)

        def _finish(completed: asyncio.Task[None]) -> None:
            self._pending_tasks.discard(completed)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(_finish)

    async def _resolve_pending_for_session(self, session_code: str) -> None:
        lock = self._session_locks.setdefault(session_code, asyncio.Lock())
        async with lock:
            session = self._sessions.get(session_code)
            if session is None:
                return
            for key, challenge in tuple(self._pending_challenges.items()):
                if challenge.session_code != session_code:
                    continue
                cached = self._original_messages.get(challenge.target_message_id)
                if cached is None:
                    continue
                if session.evidence is None:
                    await self._resolve(session, challenge, cached)
                    session = self._sessions.get(session_code)
                self._pending_challenges.pop(key, None)
                if session is None:
                    return

    async def handle_event(
        self,
        event: object,
        *,
        bot: OneBotBot | None = None,
    ) -> ReplyEvidence | None:
        """Consume one admitted OneBot group event without sending anything."""
        if not isinstance(event, GroupMessageEvent):
            return None
        now = self._now()
        self._prune(now)
        try:
            original = self._cache_original(event, now)
        except (AttributeError, TypeError, ValueError):
            return None
        if original is not None:
            return await self._resolve_pending(original, bot=bot)

        try:
            challenge = self._build_challenge(event, now)
        except (AttributeError, TypeError, ValueError):
            return None
        if challenge is None:
            return None
        return await self._handle_challenge(challenge, bot=bot)

    async def _handle_challenge(
        self,
        challenge: _Challenge,
        *,
        bot: OneBotBot | None,
    ) -> ReplyEvidence | None:
        session = self._sessions.get(challenge.session_code)
        if session is None:
            self._remember_challenge(challenge)
            return None
        lock = self._session_locks.setdefault(challenge.session_code, asyncio.Lock())
        async with lock:
            now = self._now()
            self._prune(now)
            session = self._sessions.get(challenge.session_code)
            if session is None:
                return None
            if not self._session_current(session, now):
                self._sessions.pop(challenge.session_code, None)
                return None
            if session.evidence is not None:
                return self._existing_challenge_evidence(session, challenge)
            cached = self._original_messages.get(challenge.target_message_id)
            if cached is None:
                self._remember_challenge(challenge)
                return None
            return await self._resolve(session, challenge, cached, bot=bot)

    def _existing_challenge_evidence(
        self,
        session: ReplyEvidenceSession,
        challenge: _Challenge,
    ) -> ReplyEvidence | None:
        cached = self._original_messages.get(challenge.target_message_id)
        if session.evidence is not None and self._matches_existing_evidence(
            session.evidence,
            challenge,
            cached,
        ):
            return session.evidence
        return None

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError(  # noqa: TRY003
                "clock must return a timezone-aware datetime"
            )
        return now

    @staticmethod
    def _trim(container: OrderedDict[Any, Any], limit: int) -> None:
        while len(container) > limit:
            container.popitem(last=False)

    def _prune(self, now: datetime) -> None:
        for code, session in tuple(self._sessions.items()):
            if session.expires_at <= now or session.connection_generation != self._connection_generation:
                self._sessions.pop(code, None)
        for message_id, cached in tuple(self._original_messages.items()):
            if cached.observed_at + SESSION_TTL <= now or cached.generation != self._connection_generation:
                self._original_messages.pop(message_id, None)
        for key, challenge in tuple(self._pending_challenges.items()):
            if challenge.observed_at + SESSION_TTL <= now or challenge.generation != self._connection_generation:
                self._pending_challenges.pop(key, None)
        for code, lock in tuple(self._session_locks.items()):
            if code not in self._sessions and not lock.locked():
                self._session_locks.pop(code, None)

    def _session_current(self, session: ReplyEvidenceSession, now: datetime) -> bool:
        return (
            self._sessions.get(session.session_code) is session
            and session.connection_generation == self._connection_generation
            and session.expires_at > now
        )

    def _original_current(self, cached: _OriginalMessage, now: datetime) -> bool:
        return (
            self._original_messages.get(cached.message_id) is cached
            and cached.generation == self._connection_generation
            and cached.observed_at + SESSION_TTL > now
        )

    def _challenge_current(self, challenge: _Challenge, now: datetime) -> bool:
        return (
            challenge.generation == self._connection_generation
            and challenge.observed_at + SESSION_TTL > now
        )

    def _cache_original(
        self,
        event: GroupMessageEvent,
        now: datetime,
    ) -> _OriginalMessage | None:
        message = getattr(event, "original_message", None)
        if not isinstance(message, Message):
            return None
        command = _is_bind_command(message, self.official_bot_qq)
        if command is None:
            return None
        group_id = _event_group_id(event)
        member_qq = _event_member_id(event)
        message_id = _message_id(getattr(event, "message_id", None))
        raw_message = getattr(event, "raw_message", None)
        if (
            group_id is None
            or member_qq is None
            or member_qq == self.official_bot_qq
            or message_id is None
            or not isinstance(raw_message, str)
        ):
            return None
        cached = _OriginalMessage(
            message_id=message_id,
            group_id=group_id,
            member_qq=member_qq,
            command=command,
            raw_message=raw_message,
            signature=_message_signature(message),
            observed_at=now,
            generation=self._connection_generation,
        )
        existing = self._original_messages.get(message_id)
        if existing is None:
            self._original_messages[message_id] = cached
            self._trim(self._original_messages, _MAX_ORIGINAL_MESSAGES)
            return cached
        return existing

    def _build_challenge(  # noqa: PLR0911
        self,
        event: GroupMessageEvent,
        now: datetime,
    ) -> _Challenge | None:
        group_id = _event_group_id(event)
        message_id = _message_id(getattr(event, "message_id", None))
        event_user_id = _canonical_id(getattr(event, "user_id", None))
        sender = getattr(event, "sender", None)
        sender_user_id = _canonical_id(getattr(sender, "user_id", None))
        if (
            group_id is None
            or message_id is None
            or event_user_id != self.official_bot_qq
            or sender_user_id != self.official_bot_qq
        ):
            return None
        code = _challenge_code(event)
        if code is None:
            return None
        original_message = getattr(event, "original_message", None)
        if not isinstance(original_message, Message):
            return None
        target_id = _native_reply_id(original_message)
        if target_id is None:
            return None
        reply = getattr(event, "reply", None)
        if reply is not None and not isinstance(reply, Reply):
            return None
        reply_message_type: str | None = None
        reply_member_qq: str | None = None
        reply_group_id: str | None = None
        reply_signature: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] | None = None
        if reply is not None:
            reply_id = _message_id(getattr(reply, "message_id", None))
            if reply_id != target_id:
                return None
            reply_message_type = getattr(reply, "message_type", None)
            if not isinstance(reply_message_type, str):
                return None
            reply_sender = getattr(reply, "sender", None)
            reply_member_qq = _canonical_id(getattr(reply_sender, "user_id", None))
            reply_group_id = _canonical_id(getattr(reply, "group_id", None))
            reply_message = getattr(reply, "message", None)
            if not isinstance(reply_message, Message) or reply_member_qq is None:
                return None
            reply_signature = _message_signature(reply_message)
        return _Challenge(
            session_code=code,
            target_message_id=target_id,
            challenge_message_id=message_id,
            group_id=group_id,
            reply_message_type=reply_message_type,
            reply_member_qq=reply_member_qq,
            reply_group_id=reply_group_id,
            reply_signature=reply_signature,
            observed_at=now,
            generation=self._connection_generation,
        )

    def _remember_challenge(self, challenge: _Challenge) -> None:
        key = (
            challenge.session_code,
            challenge.target_message_id,
            challenge.challenge_message_id,
        )
        self._pending_challenges[key] = challenge
        self._trim(self._pending_challenges, _MAX_PENDING_CHALLENGES)

    async def _resolve_pending(
        self,
        original: _OriginalMessage,
        *,
        bot: OneBotBot | None = None,
    ) -> ReplyEvidence | None:
        result: ReplyEvidence | None = None
        for key, challenge in tuple(self._pending_challenges.items()):
            if challenge.target_message_id != original.message_id:
                continue
            lock = self._session_locks.setdefault(
                challenge.session_code, asyncio.Lock()
            )
            async with lock:
                session = self._sessions.get(challenge.session_code)
                if session is None:
                    continue
                if session.evidence is not None:
                    self._pending_challenges.pop(key, None)
                    if result is None:
                        result = session.evidence
                    continue
                resolved = await self._resolve(
                    session,
                    challenge,
                    original,
                    bot=bot,
                )
                self._pending_challenges.pop(key, None)
                if resolved is not None and result is None:
                    result = resolved
        return result

    async def _resolve(  # noqa: PLR0911
        self,
        session: ReplyEvidenceSession,
        challenge: _Challenge,
        cached: _OriginalMessage,
        *,
        bot: OneBotBot | None = None,
    ) -> ReplyEvidence | None:
        now = self._now()
        if (
            not self._session_current(session, now)
            or not self._challenge_current(challenge, now)
            or not self._original_current(cached, now)
        ):
            return None
        if cached.group_id != challenge.group_id:
            return None
        try:
            payload = await self._fetch_message(challenge.target_message_id, bot=bot)
        except Exception:
            return None
        now = self._now()
        if (
            not self._session_current(session, now)
            or not self._challenge_current(challenge, now)
            or not self._original_current(cached, now)
        ):
            return None
        if not isinstance(payload, Mapping):
            return None
        try:
            valid_payload = self._verify_payload(payload, session, challenge, cached)
        except (AttributeError, TypeError, ValueError):
            return None
        if not valid_payload:
            return None
        evidence = ReplyEvidence(
            app_id=session.app_id,
            session_code=session.session_code,
            group_openid=session.group_openid,
            member_openid=session.member_openid,
            group_id=cached.group_id,
            member_qq=cached.member_qq,
            original_command=session.original_command,
            qq_message_id=session.qq_message_id,
            onebot_original_message_id=cached.message_id,
            challenge_message_id=challenge.challenge_message_id,
            connection_generation=session.connection_generation,
        )
        self._sessions[session.session_code] = replace(session, evidence=evidence)
        return evidence

    @staticmethod
    def _matches_existing_evidence(
        evidence: ReplyEvidence,
        challenge: _Challenge,
        cached: _OriginalMessage | None,
    ) -> bool:
        return all(
            (
                evidence.onebot_original_message_id == challenge.target_message_id,
                evidence.challenge_message_id == challenge.challenge_message_id,
                evidence.group_id == challenge.group_id,
                challenge.reply_message_type in (None, "group"),
                challenge.reply_member_qq in (None, evidence.member_qq),
                cached is None
                or challenge.reply_signature is None
                or challenge.reply_signature == cached.signature,
                challenge.reply_group_id in (None, evidence.group_id),
            )
        )

    def _verify_payload(  # noqa: PLR0911
        self,
        payload: Mapping[str, object],
        session: ReplyEvidenceSession,
        challenge: _Challenge,
        cached: _OriginalMessage,
    ) -> bool:
        message_id = _message_id(payload.get("message_id"))
        if message_id != challenge.target_message_id:
            return False
        if payload.get("message_type") != "group":
            return False
        group_id = _canonical_id(payload.get("group_id"))
        if group_id is None or group_id != challenge.group_id or group_id != cached.group_id:
            return False
        sender = payload.get("sender")
        if not isinstance(sender, Mapping):
            return False
        member_qq = _canonical_id(sender.get("user_id"))
        if member_qq is None or member_qq != cached.member_qq:
            return False
        payload_message = _message_from_payload(payload.get("message"))
        if payload_message is None:
            return False
        if _message_signature(payload_message) != cached.signature:
            return False
        if session.original_command != cached.command:
            return False
        if _message_text(payload_message) != session.original_command:
            return False
        if not _has_official_mention(payload_message, self.official_bot_qq):
            return False
        raw_message = payload.get("raw_message")
        if not isinstance(raw_message, str) or raw_message != cached.raw_message:
            return False
        if challenge.reply_message_type != "group":
            return challenge.reply_message_type is None and challenge.reply_signature is None
        if challenge.reply_member_qq != member_qq:
            return False
        if challenge.reply_group_id is not None and challenge.reply_group_id != group_id:
            return False
        return challenge.reply_signature == _message_signature(payload_message)

    async def _fetch_message(
        self,
        message_id: int,
        *,
        bot: OneBotBot | None,
    ) -> Mapping[str, object]:
        """Fetch through the receiving OneBot bot when one is available."""
        if bot is not None:
            if not isinstance(bot, OneBotBot):
                raise RuntimeError("receiving OneBot bot is required")  # noqa: TRY003
            payload = await bot.call_api("get_msg", message_id=message_id)
            if not isinstance(payload, Mapping):
                raise TypeError("message payload is not a mapping")  # noqa: TRY003
            return payload
        return await self._message_fetcher(message_id)


class _RuntimeCollectors:
    def __init__(self) -> None:
        self.values: tuple[ReplyEvidenceCollector, ...] = ()
        self.receiver: EvidenceReceiver | None = None


_runtime_collectors = _RuntimeCollectors()


def get_runtime_collectors() -> tuple[ReplyEvidenceCollector, ...]:
    """Return the currently configured collectors as an immutable snapshot."""
    return _runtime_collectors.values


def set_runtime_collectors(
    collectors: Sequence[ReplyEvidenceCollector],
) -> None:
    """Replace runtime collectors while retaining every configured app."""
    _runtime_collectors.values = tuple(collectors)


def register_evidence_receiver(receiver: EvidenceReceiver | None) -> None:
    """Install the one current evidence receiver for runtime assembly."""
    _runtime_collectors.receiver = receiver


reply_evidence_matcher = on_message(priority=1, block=False)


@reply_evidence_matcher.handle()
async def _consume_reply_evidence(
    bot: Bot,
    event: Event,
) -> None:
    """Consume evidence only after the global group admission gate."""
    if not isinstance(bot, OneBotBot) or not isinstance(event, GroupMessageEvent):
        return
    receiver = _runtime_collectors.receiver
    for collector in get_runtime_collectors():
        evidence = await collector.handle_event(event, bot=bot)
        if evidence is not None and receiver is not None:
            await receiver(evidence)


__all__ = [
    "ReplyEvidence",
    "ReplyEvidenceCollector",
    "ReplyEvidenceSession",
    "SessionCodeCollisionError",
    "get_runtime_collectors",
    "register_evidence_receiver",
    "reply_evidence_matcher",
    "set_runtime_collectors",
]
