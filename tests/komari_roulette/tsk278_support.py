# ruff: noqa: RUF001, RUF002, RUF003  # ｜ 是玩家行列分隔符（规范字符，非代码符号）
"""Self-contained, service-free helpers for the TSK-278 test baseline.

Everything here runs without a NoneBot driver, without a real PostgreSQL,
without redis and without the network.  QQ events are built with
``model_construct``, the bot is a minimal subclass, and every collaborator
of the handler/delivery under test is a recording fake.

No TSK-278 production symbol is imported at module import time on purpose:
the RED baseline must fail on the specific missing seam, not on this helper
module.  Tests that need a TSK-278 symbol import it inside the test function
/ module they own.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, cast

from nonebot.adapters.qq import Bot as QQBot
from nonebot.adapters.qq.adapter import Adapter as QQAdapter
from nonebot.adapters.qq.config import BotInfo, Intents
from nonebot.adapters.qq.event import (
    C2CMessageCreateEvent,
    DirectMessageCreateEvent,
    GroupAtMessageCreateEvent,
    GroupMessageCreateEvent,
)
from nonebot.adapters.qq.message import Message
from nonebot.adapters.qq.models import Action, Button, Permission, RenderData
from nonebot.adapters.qq.models.qq import GroupMemberAuthor

from komari_bot.plugins.group_admission.qq import (
    QQ_ADMISSION_STATE_KEY,
    QQAdmissionToken,
)
from komari_bot.plugins.komari_roulette import (
    CommandReceipt,
    FulfillmentClaim,
    FulfillmentState,
    Observation,
    ReplyGameView,
    ReplyPlayer,
    ReplyProjection,
    ReplyProjectionContext,
)

APP_ID = "tsk278-app"
GROUP_OPENID = "group-1"


# ---------------------------------------------------------------------------
# Real admission handoff (TSK-274 gate token; not faked)
# ---------------------------------------------------------------------------


def business_token(
    *,
    scope: str = "business",
    member_openid: str = "member-1",
    qq_message_id: str = "msg-1",
    app_id: str = APP_ID,
    group_openid: str = GROUP_OPENID,
    effective_policy_revision: int = 1,
    connection_generation: int = 0,
) -> QQAdmissionToken:
    """A real TSK-274 ``QQAdmissionToken`` as the preprocessor would leave in
    ``event.state`` for an admitted group-@ message.

    ``app_id`` / ``group_openid`` are overridable so tests can build a token
    whose identity does **not** match the event (the handler must reject it).
    """
    return QQAdmissionToken(
        scope=cast("Any", scope),
        app_id=app_id,
        group_openid=group_openid,
        member_openid=member_openid,
        qq_message_id=qq_message_id,
        group_id=None,
        member_qq=None,
        effective_policy_revision=effective_policy_revision,
        connection_generation=connection_generation,
        claim=None,
        verified_session=None,
    )


def admission_state(
    *,
    token: QQAdmissionToken | None = None,
) -> dict[str, Any]:
    """The ``state`` dict a handler receives for an admitted group-@ message.

    Defaults to a real admitted token; pass ``token=None`` explicitly only if
    a test wants a handoff carrying no token (not used by the suite).
    """
    if token is None:
        token = business_token()
    return {QQ_ADMISSION_STATE_KEY: token}


# ---------------------------------------------------------------------------
# QQ event / bot construction (no driver, no NoneBot init)
# ---------------------------------------------------------------------------


class FakeQQBot(QQBot):
    """Minimal QQ Bot used as the handler's ``sender`` stand-in."""

    def __init__(self, self_id: str = APP_ID) -> None:
        adapter = cast("QQAdapter", QQAdapter.__new__(QQAdapter))
        super().__init__(
            adapter,
            self_id,
            BotInfo(
                id=self_id,
                token="test-token",
                secret="test-secret",
                intent=Intents(c2c_group_at_messages=True),
                use_websocket=False,
            ),
        )


def _member(
    member_openid: str,
    *,
    username: str,
) -> GroupMemberAuthor:
    return GroupMemberAuthor.model_construct(
        id=f"author-{member_openid}",
        bot=False,
        member_openid=member_openid,
        member_role="member",
        union_openid=None,
        username=username,
    )


def make_group_at_event(
    content: str,
    *,
    message_id: str = "msg-1",
    group_openid: str = GROUP_OPENID,
    member_openid: str = "member-1",
    author_name: str = "小明",
    mentions: list[dict[str, Any]] | None = None,
    to_me: bool = True,
) -> GroupAtMessageCreateEvent:
    """Build a strict QQ ``GROUP_AT_MESSAGE_CREATE`` event by hand.

    ``content`` keeps the adapter's leading-space convention so
    ``event.get_message()`` strips it exactly like the real adapter.
    """
    return GroupAtMessageCreateEvent.model_construct(
        id=message_id,
        content=f" {content}",
        timestamp="2026-09-08T12:00:00+08:00",
        mentions=mentions or [],
        attachments=None,
        message_scene=None,
        message_type=None,
        msg_idx=None,
        msg_elements=None,
        event_id=message_id,
        to_me=to_me,
        reply=None,
        author=_member(member_openid, username=author_name),
        group_id=f"qq-group-{group_openid}",
        group_openid=group_openid,
    )


def make_real_group_at_event(
    content: str,
    *,
    message_id: str = "msg-1",
    group_openid: str = GROUP_OPENID,
    member_openid: str = "member-1",
    author_name: str = "小明",
    app_id: str = APP_ID,
    timestamp: int = 1757300000,
) -> GroupAtMessageCreateEvent:
    """A strict QQ ``GROUP_AT_MESSAGE_CREATE`` event built the way the real
    adapter builds it: from a *minimal real payload* via ``model_validate``.

    The real model has **no** ``mentions`` field (``event.mentions is None``);
    @s only exist as ``mention_user`` / ``mention_everyone`` segments parsed
    out of ``content`` (``<@!openid>`` / ``<@!all>``).  ``content`` must be the
    raw QQ content without the adapter's leading-space convention.
    """
    payload: dict[str, Any] = {
        "id": message_id,
        "time": timestamp,
        "type": "group_at_message_create",
        "detail_type": "group",
        "self": {"platform": "qq", "user_id": app_id},
        "content": content,
        "timestamp": "2026-09-08T12:00:00+08:00",
        "author": {
            "id": f"author-{member_openid}",
            "member_openid": member_openid,
            "bot": False,
            "member_role": "member",
            "username": author_name,
        },
        "group_openid": group_openid,
        "group_id": f"qq-group-{group_openid}",
    }
    return GroupAtMessageCreateEvent.model_validate(payload)


def event_mention_count(event: Any) -> int:
    """Number of real @ segments in a parsed QQ message.

    The handler's ``target_mention_count`` fingerprint must count these, not
    ``event.mentions`` (which the real model does not carry).
    """
    return sum(
        1
        for segment in event.get_message()
        if segment.type in {"mention_user", "mention_everyone"}
    )


def make_plain_group_event(
    content: str,
    *,
    message_id: str = "msg-1",
    group_openid: str = GROUP_OPENID,
    member_openid: str = "member-1",
    author_name: str = "小明",
) -> GroupMessageCreateEvent:
    """A plain (non-@) group message: must never trigger the handler."""
    return GroupMessageCreateEvent.model_construct(
        id=message_id,
        content=f" {content}",
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
        author=_member(member_openid, username=author_name),
        group_id=f"qq-group-{group_openid}",
        group_openid=group_openid,
    )


def make_c2c_event(
    content: str,
    *,
    message_id: str = "msg-1",
    member_openid: str = "member-1",
    author_name: str = "小明",
) -> C2CMessageCreateEvent:
    """A C2C (private) message: must never trigger the handler."""
    return C2CMessageCreateEvent.model_construct(
        id=message_id,
        content=f" {content}",
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
        author=_member(member_openid, username=author_name),
        msg_id=message_id,
        msg_seq=1,
        msg_timestamp="2026-09-08T12:00:00+08:00",
        private=True,
    )


def make_direct_event(
    content: str,
    *,
    message_id: str = "msg-1",
    member_openid: str = "member-1",
    author_name: str = "小明",
) -> DirectMessageCreateEvent:
    """A guild direct message: must never trigger the handler."""
    return DirectMessageCreateEvent.model_construct(
        id=message_id,
        content=f" {content}",
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
        author=_member(member_openid, username=author_name),
    )


# ---------------------------------------------------------------------------
# Frozen projection fixtures (speak only the TSK-276 public types)
# ---------------------------------------------------------------------------

NAMES = {1: "小明", 2: "小红", 3: "小白"}


def player(
    seq: int,
    *,
    name: str | None = None,
    alive: bool = True,
    inventory: tuple[tuple[str, int], ...] = (),
    pending_lock: bool = False,
    member_openid: str | None = None,
) -> ReplyPlayer:
    return ReplyPlayer(
        join_seq=seq,
        display_name=name or NAMES.get(seq, f"玩家{seq}"),
        alive=alive,
        inventory_counts=inventory,
        member_openid=member_openid or f"member-{seq}",
        pending_lock=pending_lock,
    )


def game_view(
    *,
    host_seq: int | None = None,
    remaining_total: int = 4,
    remaining_live: int = 1,
    remaining_blank: int = 3,
    hit_percent: float | None = 25.0,
    pending_reward_count: int = 0,
    pending_burst: bool = False,
    pending_lock_seqs: tuple[int, ...] = (),
    pending_lock_players: tuple[ReplyPlayer, ...] = (),
    active_lock_player: ReplyPlayer | None = None,
) -> ReplyGameView:
    return ReplyGameView(
        host_seq=host_seq,
        chamber_remaining_total=remaining_total,
        chamber_remaining_live=remaining_live,
        chamber_remaining_blank=remaining_blank,
        hit_probability_percent=hit_percent,
        pending_reward_count=pending_reward_count,
        pending_burst=pending_burst,
        pending_lock_player_seqs=pending_lock_seqs,
        pending_lock_players=pending_lock_players,
        active_lock_player=active_lock_player,
    )


def context(
    *,
    result_code: str = "shot",
    game_id: str | None = "game-1",
    lifecycle: str | None = "active",
    phase: str | None = "follow_up",
    state_revision: int | None = 7,
    turn_seq: int | None = 3,
    actor_member_openid: str = "member-1",
    target_mention_count: int = 1,
    details: dict[str, Any] | None = None,
    players: tuple[ReplyPlayer, ...] = (),
    current_player: ReplyPlayer | None = None,
    winner: ReplyPlayer | None = None,
    winner_group_wins: int | None = None,
    target_player: ReplyPlayer | None = None,
    target_member_openid: str | None = None,
    reward_player: ReplyPlayer | None = None,
    lock_target_player: ReplyPlayer | None = None,
    mention_target: ReplyPlayer | None = None,
    mention_reason: str | None = None,
    view: ReplyGameView | None = None,
) -> ReplyProjectionContext:
    return ReplyProjectionContext(
        result_code=result_code,
        game_id=game_id,
        lifecycle=lifecycle,
        phase=phase,
        state_revision=state_revision,
        turn_seq=turn_seq,
        actor_member_openid=actor_member_openid,
        target_mention_count=target_mention_count,
        details=details or {},
        players=players,
        current_player=current_player,
        winner=winner,
        winner_group_wins=winner_group_wins,
        target_player=target_player,
        target_member_openid=target_member_openid,
        reward_player=reward_player,
        lock_target_player=lock_target_player,
        mention_target=mention_target,
        mention_reason=mention_reason,
        game_view=view,
    )


def projection(
    body: str,
    *,
    mention_member_openid: str | None = None,
    mention_display_name: str | None = None,
    keyboard_spec: str | None = '{"rows": []}',
) -> ReplyProjection:
    """Frozen projection carrying the scalar metadata TSK-276 stores.

    ``keyboard`` is the JSON object spec string produced by ``build_keyboard``
    and consumed by ``keyboard_from_spec`` (see TSK-278-contract.md section 5);
    it is always present, the empty object meaning no buttons.
    """
    metadata: dict[str, Any] = {"keyboard": keyboard_spec}
    if mention_member_openid is not None:
        metadata["mention_member_openid"] = mention_member_openid
    if mention_display_name is not None:
        metadata["mention_display_name"] = mention_display_name
    return ReplyProjection(body=body, metadata=metadata)


def receipt(
    *,
    receipt_id: str = "receipt-1",
    app_id: str = APP_ID,
    group_openid: str = GROUP_OPENID,
    inbound_msg_id: str = "msg-1",
    result_code: str = "shot",
    game_id: str | None = "game-1",
    state_revision: int | None = 7,
    turn_seq: int | None = 3,
    reply: ReplyProjection | None = None,
    fingerprint: dict[str, Any] | None = None,
) -> CommandReceipt:
    return CommandReceipt(
        receipt_id=receipt_id,
        app_id=app_id,
        group_openid=group_openid,
        inbound_msg_id=inbound_msg_id,
        fingerprint=fingerprint or {"intent": "shoot"},
        result_code=result_code,
        game_id=game_id,
        state_revision=state_revision,
        turn_seq=turn_seq,
        reply=reply or projection("> 测试正文。"),
    )


def claim(
    receipt_id: str = "receipt-1",
    state: FulfillmentState = FulfillmentState.PENDING_CONFIRMATION,
) -> FulfillmentClaim:
    return FulfillmentClaim(receipt_id=receipt_id, state=state)


# ---------------------------------------------------------------------------
# Real QQ payload builders (no TSK-278 import; adapter classes only)
# ---------------------------------------------------------------------------


def make_button(
    label: str,
    data: str,
    *,
    action_type: int = 2,
    permission_type: int = 2,
    reply: bool = False,
    enter: bool = False,
) -> Button:
    return Button(
        render_data=RenderData(label=label),
        action=Action(
            type=action_type,
            permission=Permission(type=permission_type),
            data=data,
            reply=reply,
            enter=enter,
        ),
    )


def build_real_keyboard(spec: str) -> Any:
    """Materialize a real QQ MessageKeyboard from a JSON spec.

    This mirrors what TSK-278 ``keyboard_from_spec`` must do; it lets the
    delivery tests assert on the *real* adapter payload without importing the
    missing seam.  Only the canonical object form ``{"rows": [...]}`` is
    accepted — there is no historical bare-``[]`` fallback (see
    TSK-278-contract.md section 5).
    """
    from nonebot.adapters.qq.models import (
        InlineKeyboard,
        InlineKeyboardRow,
        MessageKeyboard,
    )

    parsed = json.loads(spec)
    rows = []
    for row_spec in parsed["rows"]:
        if not isinstance(row_spec, list):
            msg = (
                "canonical keyboard spec rows are button-object lists, "
                f"got {row_spec!r}"
            )
            raise TypeError(msg)
        rows.append(
            InlineKeyboardRow(
                buttons=[make_button(b["label"], b["data"]) for b in row_spec]
            )
        )
    return MessageKeyboard(content=InlineKeyboard(rows=rows))


def build_real_message(body: str, keyboard_spec: str = '{"rows": []}') -> Message:
    """Real QQ ``Message``: one markdown segment plus a keyboard only with rows.

    TSK-266 1F / TSK-278-contract.md: a keyboard with no buttons is not a
    keyboard field at all, so a spec whose ``rows`` are empty yields a
    markdown-only payload (no empty keyboard segment).
    """
    from nonebot.adapters.qq.message import MessageSegment

    message = Message()
    message += MessageSegment.markdown(body)
    if json.loads(keyboard_spec)["rows"]:
        message += MessageSegment.keyboard(build_real_keyboard(keyboard_spec))
    return message


def has_keyboard_segment(message: Any) -> bool:
    """True when a real QQ ``Message`` carries a keyboard segment.

    ``message["keyboard"]`` cannot be used here: the adapter's
    ``Message.__getitem__`` returns an empty ``Message`` (not ``None``) when no
    segment of that type exists, so absence must be tested against the real
    members by segment type (TSK-278-contract.md section 5).
    """
    if isinstance(message, Message):
        return any(segment.type == "keyboard" for segment in message)
    return False


def assert_no_keyboard_segment(message: Any) -> None:
    """A no-buttons payload must not carry an empty keyboard field."""
    assert not has_keyboard_segment(message), (
        "empty-buttons message must not carry a keyboard segment"
    )


def message_markdown_content(message: Any) -> str:
    """Extract the markdown text from a real QQ Message payload."""
    if isinstance(message, Message):
        for segment in message:
            if segment.type == "markdown":
                markdown = segment.data.get("markdown", {})
                content = getattr(markdown, "content", None)
                if content is None and isinstance(markdown, dict):
                    content = markdown.get("content")
                return str(content)
        raise AssertionError("no markdown segment in message")  # noqa: TRY003
    return str(getattr(message, "content", message))


def message_keyboard_rows(message: Any) -> list[list["ButtonSpec"]]:
    """Extract flattened keyboard rows from a real QQ Message payload."""
    if isinstance(message, Message):
        for segment in message:
            if segment.type == "keyboard":
                return flatten_keyboard(segment.data["keyboard"])
        raise AssertionError("no keyboard segment in message")  # noqa: TRY003
    return flatten_keyboard(message)


# ---------------------------------------------------------------------------
# Mention-tag inspection helpers
# ---------------------------------------------------------------------------

MENTION_TAG_RE = re.compile(r"<qqbot-at-user id=\"([^\"]*)\"\s*/>")


def mention_tags(body: str) -> list[str]:
    """Every ``<qqbot-at-user id="..." />`` tag (openid, in body order)."""
    return MENTION_TAG_RE.findall(body)


def assert_single_mention_tag(body: str, openid: str) -> None:
    """Exactly one native mention tag for ``openid``, in the body."""
    tags = mention_tags(body)
    assert tags == [openid], f"expected single {openid!r} tag, got {tags!r} in {body!r}"


def assert_no_mention_tag(body: str) -> None:
    assert MENTION_TAG_RE.search(body) is None, f"unexpected mention tag in {body!r}"


def mention_tag_position(body: str, openid: str) -> int:
    """Index of the mention tag for ``openid`` (requires it to exist)."""
    match = re.search(
        rf'<qqbot-at-user id="{re.escape(openid)}"\s*/>',
        body,
    )
    assert match is not None, f"no mention tag for {openid!r} in {body!r}"
    return match.start()


def assert_no_member_openid(body: str, *openids: str) -> None:
    """Openids may appear only inside the platform mention tag."""
    stripped = MENTION_TAG_RE.sub("", body)
    for openid in openids:
        assert openid not in stripped, (
            f"member_openid leaked outside a mention tag: {openid!r} in {body!r}"
        )


def assert_no_player_numbers(body: str, *numbers: int) -> None:
    """普通正文不得出现编号式玩家标识行（``- N｜名字`` 形式）。

    编号标识符只允许出现在专用区域（可上锁/可转让）；弹仓计数、
    “道具 N”等数量不是玩家编号，一律放行。
    """
    for number in numbers:
        pattern = rf"(?m)^- {number}｜"
        assert re.search(pattern, body) is None, (
            f"player number {number} leaked into ordinary body: {body!r}"
        )


def assert_body_has_markdown_structure(
    body: str,
    *,
    dividers: int,
    blockquote: bool,
    bold: bool,
    roster: bool,
) -> None:
    lines = body.splitlines()
    if dividers:
        divider_lines = [line for line in lines if line.strip() == "***"]
        assert len(divider_lines) == dividers, f"expected {dividers} '***', got {divider_lines}"
    else:
        assert "***" not in body
    assert (any(line.startswith("> ") for line in lines)) is blockquote
    assert ("**" in body) is bold
    roster_lines = [line for line in lines if line.startswith("- ")]
    assert bool(roster_lines) is roster


# ---------------------------------------------------------------------------
# Recording fakes
# ---------------------------------------------------------------------------


class FakeCommandService:
    """Recording stand-in for RouletteCommandService.

    Implements the calls used by the handler and the delivery:
    observe_current / execute_group_command / claim_fulfillment /
    mark_delivered / mark_not_delivered.  Behavior is configured per test;
    everything is recorded for assertions.  PG-backed tests use the real
    service instead.
    """

    def __init__(self) -> None:
        self.observe_calls: list[Any] = []
        self.execute_calls: list[tuple[Any, Observation | None]] = []
        self.claim_calls: list[str] = []
        self.mark_delivered_calls: list[tuple[Any, str]] = []
        self.mark_not_delivered_calls: list[Any] = []
        self.observation: Observation | None = None
        self.receipt: CommandReceipt | None = None
        self.claim_result: FulfillmentClaim | None = None
        self.claim_error: BaseException | None = None
        self.mark_delivered_error: BaseException | None = None

    async def observe_current(self, group: Any) -> Observation | None:
        self.observe_calls.append(group)
        return self.observation

    async def execute_group_command(
        self,
        request: Any,
        *,
        observation: Observation | None = None,
    ) -> CommandReceipt:
        self.execute_calls.append((request, observation))
        if self.receipt is None:
            raise AssertionError("receipt not configured")  # noqa: TRY003
        return self.receipt

    async def claim_fulfillment(self, receipt_id: str) -> FulfillmentClaim | None:
        self.claim_calls.append(receipt_id)
        if self.claim_error is not None:
            raise self.claim_error
        return self.claim_result

    async def mark_delivered(
        self,
        claim: FulfillmentClaim,
        *,
        platform_message_id: str,
    ) -> None:
        self.mark_delivered_calls.append((claim, platform_message_id))
        if self.mark_delivered_error is not None:
            raise self.mark_delivered_error

    async def mark_not_delivered(self, claim: FulfillmentClaim) -> None:
        self.mark_not_delivered_calls.append(claim)


class FakeSender:
    """Replaceable QQ sender.

    Modes:
    - ``success``: records one network call and returns the platform id.
    - ``fail_before_send``: raises ``exc`` *before* any network call
      (the typed ``SendNotAcceptedError`` path).
    - ``fail_after_send``: records one network call then raises ``exc``
      (timeout / connection crash / generic failure path).
    - ``cancel``: records one network call then raises CancelledError.
    """

    def __init__(
        self,
        *,
        mode: str = "success",
        exc: BaseException | None = None,
        result: object = "qq-platform-msg-1",
    ) -> None:
        self.mode = mode
        self.exc = exc
        self.result = result
        self.calls: list[dict[str, Any]] = []
        self.network_calls: list[dict[str, Any]] = []

    async def send_to_group(
        self,
        group_openid: str,
        message: Any,
        *,
        msg_id: str | None = None,
        msg_seq: int | None = None,
        **kwargs: Any,
    ) -> Any:
        payload = {
            "group_openid": group_openid,
            "message": message,
            "msg_id": msg_id,
            "msg_seq": msg_seq,
            **kwargs,
        }
        self.calls.append(payload)
        if self.mode == "fail_before_send":
            assert self.exc is not None, "fail_before_send needs exc"
            raise self.exc
        self.network_calls.append(payload)
        if self.mode == "fail_after_send":
            assert self.exc is not None, "fail_after_send needs exc"
            raise self.exc
        if self.mode == "cancel":
            raise asyncio.CancelledError
        return self.result


class FakeDelivery:
    """Recording stand-in for RouletteDelivery (injected seam).

    Outcomes are not asserted in handler tests — only that deliver was or was
    not called — so this stub returns a plain marker and records calls.
    """

    def __init__(self) -> None:
        self.deliver_calls: list[tuple[Any, Any]] = []

    async def deliver(self, receipt: Any, sender: Any) -> Any:
        self.deliver_calls.append((receipt, sender))
        return None


# ---------------------------------------------------------------------------
# Keyboard inspection
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ButtonSpec:
    label: str
    data: str
    action_type: int = 2
    permission_type: int = 2
    reply: bool = False
    enter: bool = False


def flatten_keyboard(keyboard: Any) -> list[list[ButtonSpec]]:
    """Flatten an InlineKeyboard (or a MessageKeyboard wrapping one)."""
    content = getattr(keyboard, "content", None)
    if content is not None and getattr(content, "rows", None) is not None:
        rows = content.rows
    else:
        rows = keyboard.rows
    flattened: list[list[ButtonSpec]] = []
    for row in rows:
        buttons: list[ButtonSpec] = []
        for button in row.buttons:
            action = button.action
            buttons.append(
                ButtonSpec(
                    label=str(button.render_data.label),
                    data=str(action.data),
                    action_type=int(action.type),
                    permission_type=int(action.permission.type),
                    reply=bool(action.reply),
                    enter=bool(action.enter),
                )
            )
        flattened.append(buttons)
    return flattened
