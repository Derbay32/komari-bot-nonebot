"""Self-contained, service-free helpers for the TSK-278 test baseline.

Everything here runs without a NoneBot driver, without a real PostgreSQL,
without redis and without the network.  QQ events are built with
``model_construct``, the bot is a minimal subclass, and every collaborator
of the handler/delivery under test is a recording fake.

No TSK-278 production symbol is imported at module import time on purpose:
the RED baseline must fail on the specific missing seam, not on this helper
module.  Tests that need a TSK-278 symbol import it from the plugin top
level inside the test function / module they own.
"""

from __future__ import annotations

import asyncio
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
from nonebot.adapters.qq.models.qq import GroupMemberAuthor

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
) -> ReplyProjection:
    metadata: dict[str, Any] = {}
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
# Recording fakes
# ---------------------------------------------------------------------------


class FakeCommandService:
    """Recording stand-in for RouletteCommandService.

    Implements the five calls used by the handler and the delivery:
    observe_current / execute_group_command / claim_fulfillment /
    mark_delivered / mark_not_delivered.  Behavior is configured per test;
    everything is recorded for assertions.
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
        result: str = "qq-platform-msg-1",
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
    """Recording stand-in for RouletteDelivery.

    Outcomes are passed as plain strings ("DELIVERED" / "NOT_DELIVERED" /
    "UNKNOWN" / "NO_CLAIM") so this helper does not depend on the TSK-278
    production enum at import time.
    """

    def __init__(self, outcomes: list[str] | None = None) -> None:
        self.outcomes = outcomes or ["NO_CLAIM"]
        self.deliver_calls: list[tuple[Any, Any]] = []

    async def deliver(self, receipt: Any, sender: Any) -> str:
        self.deliver_calls.append((receipt, sender))
        return self.outcomes[min(len(self.deliver_calls), len(self.outcomes)) - 1]


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
    """Flatten an InlineKeyboard (or any object with rows of buttons)."""
    rows: list[list[ButtonSpec]] = []
    for row in keyboard.rows:
        buttons: list[ButtonSpec] = []
        for button in row.buttons:
            action = button.action
            permission = button.permission
            buttons.append(
                ButtonSpec(
                    label=str(button.label),
                    data=str(action.data),
                    action_type=int(action.type),
                    permission_type=int(permission.type),
                    reply=bool(action.reply),
                    enter=bool(action.enter),
                )
            )
        rows.append(buttons)
    return rows


def assert_no_player_numbers(body: str, *numbers: int) -> None:
    """普通正文不得出现独立玩家编号（1-3 位数字片段）。"""
    import re

    # 普通正文不带玩家编号（编号只出现在专用目标区）。
    for number in numbers:
        pattern = rf"(?<![\d]){number}(?![\d])"
        assert re.search(pattern, body) is None, (
            f"player number {number} leaked into ordinary body: {body!r}"
        )


def assert_no_member_openid(body: str, *openids: str) -> None:
    for openid in openids:
        assert openid not in body, f"member_openid leaked into body: {openid!r}"


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
    assert ("> " in body) is blockquote
    assert ("**" in body) is bold
    roster_lines = [line for line in lines if line.startswith("- ")]
    assert bool(roster_lines) is roster
