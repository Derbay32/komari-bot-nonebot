"""Pure state machine for one QQ Russian roulette game.

This module intentionally knows nothing about NoneBot, persistence, or message
delivery.  Adapters provide a trusted ``GameState`` and a small random source;
the state machine returns a new state and a safe reply projection.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

TURN_DURATION = timedelta(minutes=15)
MIN_PLAYERS = 2
MAX_PLAYERS = 6
CHAMBER_SIZE = 6
INITIAL_LIVE = 2
INITIAL_BLANK = 4
INVENTORY_CAPACITY = 4


class ChamberKind(StrEnum):
    """The two kinds of round in the ordered chamber."""

    LIVE = "live"
    BLANK = "blank"


class ItemType(StrEnum):
    """The four item kinds awarded by a blank round."""

    MAGNIFIER = "magnifier"
    BEER = "beer"
    BURST = "burst"
    LOCK = "lock"


ITEM_TYPES: tuple[ItemType, ...] = (
    ItemType.MAGNIFIER,
    ItemType.BEER,
    ItemType.BURST,
    ItemType.LOCK,
)
DEFAULT_ITEM_WEIGHTS: dict[ItemType, int] = dict.fromkeys(ITEM_TYPES, 1)


class RandomSource(Protocol):
    """Random operations required by the domain."""

    def chamber_order(
        self, live_count: int, blank_count: int
    ) -> Sequence[ChamberKind]: ...

    def weighted_item(self, weights: Mapping[ItemType, int]) -> ItemType: ...


@dataclass(frozen=True, slots=True)
class GroupRef:
    """Protocol-scoped group identity."""

    app_id: str
    group_openid: str

    def __post_init__(self) -> None:
        if not self.app_id.strip() or not self.group_openid.strip():
            raise ValueError("group identity must not be empty")  # noqa: TRY003


@dataclass(frozen=True, slots=True)
class PlayerRef:
    """Protocol-scoped member identity and its frozen display name."""

    app_id: str
    group_openid: str
    member_openid: str
    display_name: str

    def __post_init__(self) -> None:
        if not self.app_id.strip():
            raise ValueError("app identity must not be empty")  # noqa: TRY003
        if not self.group_openid.strip():
            raise ValueError("group identity must not be empty")  # noqa: TRY003
        if not self.member_openid.strip():
            raise ValueError("member identity must not be empty")  # noqa: TRY003
        name = self.display_name.strip()
        if not name:
            raise ValueError("display name must not be empty")  # noqa: TRY003
        object.__setattr__(self, "display_name", name)


@dataclass(frozen=True, slots=True)
class Action:
    """Immutable command value object accepted by :func:`apply_action`."""

    kind: str
    player: PlayerRef | None = None
    item: ItemType | None = None
    target_seq: int | None = None
    decision: str | None = None
    replace_item: ItemType | None = None

    @classmethod
    def create(cls, player: PlayerRef) -> Action:
        return cls("create", player=player)

    @classmethod
    def join(cls, player: PlayerRef) -> Action:
        return cls("join", player=player)

    @classmethod
    def leave(cls, player: PlayerRef) -> Action:
        return cls("leave", player=player)

    @classmethod
    def cancel(cls, player: PlayerRef) -> Action:
        return cls("cancel", player=player)

    @classmethod
    def start(
        cls,
        player: PlayerRef,
        item_weights: Mapping[ItemType, int] | None = None,
    ) -> Action:
        return cls("start", player=player, item_weights=item_weights)

    @classmethod
    def shoot(cls, player: PlayerRef) -> Action:
        return cls("shoot", player=player)

    @classmethod
    def forfeit(cls, player: PlayerRef) -> Action:
        return cls("forfeit", player=player)

    @classmethod
    def end_turn(cls, player: PlayerRef) -> Action:
        return cls("end_turn", player=player)

    @classmethod
    def reload(cls, player: PlayerRef) -> Action:
        return cls("reload", player=player)

    @classmethod
    def use_item(
        cls,
        player: PlayerRef,
        item: ItemType,
        target_seq: int | None = None,
    ) -> Action:
        return cls("use_item", player=player, item=item, target_seq=target_seq)

    @classmethod
    def discard_item(cls, player: PlayerRef, item: ItemType) -> Action:
        return cls("discard_item", player=player, item=item)

    @classmethod
    def choose_item(
        cls,
        player: PlayerRef,
        decision: str,
        replace_item: ItemType | None = None,
    ) -> Action:
        return cls(
            "choose_item",
            player=player,
            decision=decision,
            replace_item=replace_item,
        )

    @classmethod
    def transfer(cls, player: PlayerRef, target_seq: int) -> Action:
        return cls("transfer", player=player, target_seq=target_seq)

    @classmethod
    def open_item_panel(cls, player: PlayerRef) -> Action:
        return cls("open_item_panel", player=player)

    @classmethod
    def expire(cls) -> Action:
        return cls("expire")

    # ``item_weights`` is kept out of the generic dataclass fields so the
    # command value remains compact while ``Action.start`` can freeze a copy.
    item_weights: Mapping[ItemType, int] | None = None


@dataclass(frozen=True, slots=True)
class PlayerSeat:
    """A frozen roster seat with mutable-looking data copied per transition."""

    player: PlayerRef
    join_seq: int
    alive: bool = True
    inventory: Mapping[ItemType, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.join_seq <= 0:
            raise ValueError("join sequence must be positive")  # noqa: TRY003
        object.__setattr__(
            self,
            "inventory",
            MappingProxyType(dict(self.inventory)),
        )

    @property
    def app_id(self) -> str:
        return self.player.app_id

    @property
    def group_openid(self) -> str:
        return self.player.group_openid

    @property
    def member_openid(self) -> str:
        return self.player.member_openid

    @property
    def display_name(self) -> str:
        return self.player.display_name


@dataclass(frozen=True, slots=True)
class GameState:
    """Complete trusted durable facts for one game."""

    group: GroupRef
    lifecycle: str
    phase: str | None
    state_revision: int
    chamber_revision: int
    turn_seq: int
    current_player_seq: int | None
    deadline: datetime | None
    host_seq: int | None
    players: tuple[PlayerSeat, ...]
    ordered_chamber: tuple[ChamberKind, ...]
    pending_rewards: tuple[ItemType, ...]
    pending_burst: bool
    pending_locks: tuple[int, ...]
    item_weights: Mapping[ItemType, int]
    next_join_seq: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "item_weights", MappingProxyType(dict(self.item_weights))
        )

    @classmethod
    def from_trusted_snapshot(cls, snapshot: Mapping[str, Any]) -> GameState:
        """Restore a state supplied by a trusted persistence adapter.

        This method copies every collection before constructing the value
        object.  It is deliberately a narrow trusted boundary; user payloads
        must never be sent here directly.
        """

        group = cast("GroupRef", snapshot["group"])
        raw_players = cast("Sequence[Any]", snapshot.get("players", ()))
        players: list[PlayerSeat] = []
        for raw in raw_players:
            if isinstance(raw, PlayerSeat):
                players.append(
                    PlayerSeat(
                        raw.player,
                        raw.join_seq,
                        raw.alive,
                        dict(raw.inventory),
                    )
                )
                continue
            row = cast("Mapping[str, Any]", raw)
            player = cast("PlayerRef", row["player"])
            players.append(
                PlayerSeat(
                    player,
                    int(row["join_seq"]),
                    bool(row.get("alive", True)),
                    dict(cast("Mapping[ItemType, int]", row.get("inventory", {}))),
                )
            )
        raw_weights = snapshot.get("item_weights", DEFAULT_ITEM_WEIGHTS)
        weights = _normalize_weights(cast("Mapping[ItemType, int]", raw_weights))
        if weights is None:
            raise ValueError("invalid trusted item weights")  # noqa: TRY003
        next_join_seq = int(
            snapshot.get(
                "next_join_seq",
                max((seat.join_seq for seat in players), default=0) + 1,
            )
        )
        return cls(
            group=group,
            lifecycle=str(snapshot.get("lifecycle", "waiting")),
            phase=cast("str | None", snapshot.get("phase")),
            state_revision=int(snapshot.get("state_revision", 0)),
            chamber_revision=int(snapshot.get("chamber_revision", 0)),
            turn_seq=int(snapshot.get("turn_seq", 0)),
            current_player_seq=cast("int | None", snapshot.get("current_player_seq")),
            deadline=cast("datetime | None", snapshot.get("deadline")),
            host_seq=cast("int | None", snapshot.get("host_seq")),
            players=tuple(players),
            ordered_chamber=tuple(
                ChamberKind(value)
                for value in cast(
                    "Sequence[ChamberKind | str]", snapshot.get("ordered_chamber", ())
                )
            ),
            pending_rewards=tuple(
                ItemType(value)
                for value in cast(
                    "Sequence[ItemType | str]", snapshot.get("pending_rewards", ())
                )
            ),
            pending_burst=bool(snapshot.get("pending_burst", False)),
            pending_locks=tuple(
                int(value) for value in snapshot.get("pending_locks", ())
            ),
            item_weights=weights,
            next_join_seq=next_join_seq,
        )


@dataclass(frozen=True, slots=True)
class ActionResult:
    """Domain result with a trusted state and a safe reply projection."""

    ok: bool
    code: str
    reason: str | None
    state: GameState
    reply: Mapping[str, Any]


def initial_state(group: GroupRef) -> GameState:
    """Create a state with no waiting game and no active roster."""

    return GameState(
        group=group,
        lifecycle="waiting",
        phase=None,
        state_revision=0,
        chamber_revision=0,
        turn_seq=0,
        current_player_seq=None,
        deadline=None,
        host_seq=None,
        players=(),
        ordered_chamber=(),
        pending_rewards=(),
        pending_burst=False,
        pending_locks=(),
        item_weights=dict(DEFAULT_ITEM_WEIGHTS),
        next_join_seq=1,
    )


def apply_action(  # noqa: PLR0911
    state: GameState,
    action: Action,
    *,
    now: datetime,
    random_source: RandomSource,
) -> ActionResult:
    """Apply one command, returning a new state or the unchanged input state."""

    if not _state_is_valid(state):
        return _failure(state, "invalid_game_state")
    if state.lifecycle == "waiting" and state.players and _is_expired(state, now):
        return _expire_waiting(state)
    if state.lifecycle == "active" and _is_expired(state, now):
        return _expire_active(state, now)

    if action.kind == "create":
        return _create(state, action, now)
    if action.kind == "join":
        return _join(state, action, now)
    if action.kind == "leave":
        return _leave(state, action, now)
    if action.kind == "cancel":
        return _cancel(state, action)
    if action.kind == "start":
        return _start(state, action, now, random_source)
    if action.kind == "transfer":
        return _transfer(state, action, now)

    if action.kind == "expire":
        return _expire_command(state)

    if state.lifecycle == "completed":
        return _failure(state, "game_completed")
    if state.lifecycle == "cancelled":
        return _failure(state, "game_cancelled")
    if state.lifecycle == "expired":
        return _failure(state, "game_expired")
    if state.lifecycle != "active":
        return _failure(state, "no_active_game")

    if action.player is None:
        return _failure(state, "not_participant")
    actor_seq = _find_player_seq(state, action.player)
    if actor_seq is None:
        return _failure(state, "not_participant")
    actor = _seat_by_seq(state, actor_seq)
    if actor is None:
        return _failure(state, "not_participant")
    if not actor.alive:
        return _failure(state, "player_eliminated")
    if state.current_player_seq != actor_seq:
        return _failure(state, "not_current_player")

    if state.phase == "item_choice" and action.kind not in {"choose_item", "forfeit"}:
        return _failure(state, "action_not_allowed_in_phase", "item_choice_pending")
    if state.phase == "locked_turn" and action.kind not in {"shoot", "forfeit"}:
        return _failure(state, "locked_turn_restriction")

    match action.kind:
        case "shoot":
            return _shoot(state, actor_seq, now, random_source)
        case "forfeit":
            return _forfeit(state, actor_seq, now)
        case "end_turn":
            return _end_turn(state, now)
        case "reload":
            return _reload(state, now, random_source)
        case "use_item":
            return _use_item(state, action, actor_seq, now, random_source)
        case "discard_item":
            return _discard_item(state, action, actor_seq, now)
        case "choose_item":
            return _choose_item(state, action, actor_seq, now)
        case "open_item_panel":
            return _panel(state, actor_seq)
        case _:
            return _failure(state, "unknown_action")


def _create(state: GameState, action: Action, now: datetime) -> ActionResult:
    player = action.player
    if player is None or not _same_group(state.group, player):
        return _failure(state, "not_participant")
    if state.players:
        return _failure(state, "game_already_exists")
    seat = PlayerSeat(player=player, join_seq=1)
    new_state = replace(
        state,
        lifecycle="waiting",
        phase=None,
        state_revision=1,
        deadline=now + TURN_DURATION,
        host_seq=1,
        players=(seat,),
        next_join_seq=2,
        current_player_seq=None,
        turn_seq=0,
        pending_rewards=(),
        pending_locks=(),
        pending_burst=False,
    )
    return _success(new_state, "created")


def _join(  # noqa: PLR0911
    state: GameState,
    action: Action,
    now: datetime,
) -> ActionResult:
    player = action.player
    if state.lifecycle in {"cancelled", "expired"}:
        return _failure(state, "no_waiting_game")
    if state.lifecycle != "waiting":
        return _failure(state, "game_already_started")
    if not state.players:
        return _failure(state, "no_waiting_game")
    if player is None or not _same_group(state.group, player):
        return _failure(state, "not_participant")
    if _find_player_seq(state, player) is not None:
        return _failure(state, "already_joined")
    if len(state.players) >= MAX_PLAYERS:
        return _failure(state, "game_full")
    next_seq = state.next_join_seq
    seats = (*state.players, PlayerSeat(player=player, join_seq=next_seq))
    new_state = replace(
        state,
        players=seats,
        state_revision=state.state_revision + 1,
        deadline=now + TURN_DURATION,
        next_join_seq=next_seq + 1,
    )
    return _success(new_state, "joined")


def _leave(state: GameState, action: Action, now: datetime) -> ActionResult:
    if state.lifecycle != "waiting":
        return _failure(state, "game_already_started")
    player = action.player
    if player is None:
        return _failure(state, "not_participant")
    seq = _find_player_seq(state, player)
    if seq is None:
        return _failure(state, "not_participant")
    seats = tuple(seat for seat in state.players if seat.join_seq != seq)
    if not seats:
        new_state = replace(
            state,
            lifecycle="cancelled",
            phase=None,
            players=(),
            host_seq=None,
            deadline=None,
            state_revision=state.state_revision + 1,
        )
        return _success(new_state, "cancelled")
    host_seq = state.host_seq
    code = "left"
    if seq == state.host_seq:
        host_seq = seats[0].join_seq
        code = "host_transferred"
    new_state = replace(
        state,
        players=seats,
        host_seq=host_seq,
        state_revision=state.state_revision + 1,
        deadline=now + TURN_DURATION,
    )
    return _success(new_state, code)


def _cancel(state: GameState, action: Action) -> ActionResult:
    if state.lifecycle != "waiting" or not state.players:
        return _failure(state, "no_waiting_game")
    player = action.player
    if player is None or _find_player_seq(state, player) is None:
        return _failure(state, "not_participant")
    if _find_player_seq(state, player) != state.host_seq:
        return _failure(state, "not_host")
    new_state = replace(
        state,
        lifecycle="cancelled",
        phase=None,
        current_player_seq=None,
        deadline=None,
        state_revision=state.state_revision + 1,
        players=(),
        host_seq=None,
        pending_rewards=(),
        pending_burst=False,
        pending_locks=(),
    )
    return _success(new_state, "cancelled")


def _transfer(state: GameState, action: Action, now: datetime) -> ActionResult:
    if state.lifecycle != "waiting" or not state.players:
        return _failure(state, "no_waiting_game")
    player = action.player
    if player is None or _find_player_seq(state, player) is None:
        return _failure(state, "not_participant")
    if _find_player_seq(state, player) != state.host_seq:
        return _failure(state, "not_host")
    target = _seat_by_seq(state, action.target_seq)
    if target is None:
        return _failure(state, "player_seq_not_found")
    new_state = replace(
        state,
        host_seq=target.join_seq,
        deadline=now + TURN_DURATION,
        state_revision=state.state_revision + 1,
    )
    return _success(new_state, "host_transferred")


def _start(  # noqa: PLR0911
    state: GameState,
    action: Action,
    now: datetime,
    random_source: RandomSource,
) -> ActionResult:
    if state.lifecycle != "waiting" or not state.players:
        return _failure(state, "no_waiting_game")
    player = action.player
    if player is None or _find_player_seq(state, player) is None:
        return _failure(state, "not_participant")
    if _find_player_seq(state, player) != state.host_seq:
        return _failure(state, "not_host")
    if len(state.players) < MIN_PLAYERS:
        return _failure(state, "not_enough_players")
    weights = _normalize_weights(action.item_weights)
    if weights is None:
        return _failure(state, "invalid_item_weights")
    chamber = _random_chamber(random_source, INITIAL_LIVE, INITIAL_BLANK)
    if chamber is None:
        return _failure(state, "random_source_failed")
    new_state = replace(
        state,
        lifecycle="active",
        phase="first_shot",
        state_revision=state.state_revision + 1,
        chamber_revision=1,
        turn_seq=1,
        current_player_seq=state.players[0].join_seq,
        deadline=now + TURN_DURATION,
        ordered_chamber=chamber,
        pending_rewards=(),
        pending_burst=False,
        pending_locks=(),
        item_weights=weights,
    )
    return _success(new_state, "started")


def _shoot(  # noqa: PLR0911
    state: GameState,
    actor_seq: int,
    now: datetime,
    random_source: RandomSource,
) -> ActionResult:
    if state.phase not in {"first_shot", "follow_up", "locked_turn"}:
        return _failure(state, "action_not_allowed_in_phase")
    locked = state.phase == "locked_turn"
    burst = state.pending_burst
    chamber = list(state.ordered_chamber)
    chamber_revision = state.chamber_revision
    consumptions: list[ChamberKind] = []
    reload_reason: str | None = None
    auto_reloaded = False
    try:
        for _ in range(2 if burst else 1):
            if not chamber:
                order = _random_chamber(random_source, INITIAL_LIVE, INITIAL_BLANK)
                if order is None:
                    return _failure(state, "random_source_failed")
                chamber = list(order)
                chamber_revision += 1
                reload_reason = "empty"
                auto_reloaded = True
            kind = chamber.pop(0)
            consumptions.append(kind)
            chamber_revision += 1
            if kind is ChamberKind.LIVE:
                break
            reason = _normalization_reason(chamber)
            if reason is not None:
                order = _random_chamber(random_source, INITIAL_LIVE, INITIAL_BLANK)
                if order is None:
                    return _failure(state, "random_source_failed")
                chamber = list(order)
                chamber_revision += 1
                reload_reason = reason
                auto_reloaded = True
    except Exception:
        return _failure(state, "random_source_failed")

    has_live = ChamberKind.LIVE in consumptions
    base_reply: dict[str, Any] = {
        "consumptions": [kind.value for kind in consumptions],
        "consumed_kind": consumptions[0].value,
        "remaining_live": chamber.count(ChamberKind.LIVE),
        "remaining_blank": chamber.count(ChamberKind.BLANK),
        "auto_reloaded": auto_reloaded,
        "reload_reason": reload_reason,
        "previous_chamber_revision": state.chamber_revision,
        "chamber_revision": chamber_revision,
        "reward_count": 0,
    }
    new_state = replace(
        state,
        ordered_chamber=tuple(chamber),
        chamber_revision=chamber_revision,
        state_revision=state.state_revision + 1,
        pending_burst=False,
        deadline=now + TURN_DURATION,
    )

    if has_live:
        base_reply["reward_count"] = 0
        return _eliminate_current(
            new_state,
            actor_seq,
            now,
            completion_reason="shot",
            reply=base_reply,
        )
    if locked:
        base_reply["reward_count"] = 0
        return _advance_turn_result(new_state, now, base_reply, code="shot")
    if state.phase == "first_shot":
        new_state = replace(new_state, phase="follow_up")
        return _success(new_state, "shot", base_reply)

    # Follow-up rewards are sampled only after the whole action is known to be
    # blank.  Sampling every item before mutating inventory makes failures
    # atomic and keeps the durable facts unchanged.
    try:
        rewards = tuple(
            _random_item(random_source, new_state.item_weights) for _ in consumptions
        )
    except Exception:
        return _failure(state, "random_source_failed")
    updated, visible_rewards, pending = _apply_rewards(new_state, rewards)
    base_reply["reward_count"] = len(rewards)
    if pending:
        updated = replace(updated, phase="item_choice")
        base_reply.update(_item_choice_reply(updated))
    else:
        updated = replace(updated, phase="follow_up")
        base_reply["rewards"] = [item.value for item in visible_rewards]
    return _success(updated, "item_choice_pending" if pending else "shot", base_reply)


def _use_item(  # noqa: PLR0911
    state: GameState,
    action: Action,
    actor_seq: int,
    now: datetime,
    random_source: RandomSource,
) -> ActionResult:
    if state.phase not in {"first_shot", "follow_up"}:
        return _failure(state, "action_not_allowed_in_phase")
    item = action.item
    if not isinstance(item, ItemType):
        return _failure(state, "unknown_item")
    actor = _seat_by_seq(state, actor_seq)
    if actor is None or actor.inventory.get(item, 0) <= 0:
        return _failure(state, "item_not_owned")
    if item is ItemType.BURST and len(state.ordered_chamber) < 2:
        return _failure(state, "item_precondition_failed", "burst_requires_two_rounds")
    if item is ItemType.BURST and state.pending_burst:
        return _failure(state, "item_effect_conflict", "burst_already_pending")
    if item is ItemType.BEER and state.pending_burst:
        return _failure(state, "item_precondition_failed", "beer_blocked_by_burst")

    if item is ItemType.MAGNIFIER:
        chamber = state.ordered_chamber
        if not chamber:
            return _failure(state, "item_precondition_failed", "chamber_empty")
        new_state = _consume_inventory(
            state,
            actor_seq,
            item,
            now=now,
            pending_burst=state.pending_burst,
        )
        reply = {
            "item": item.value,
            "observed_kind": chamber[0].value,
            "observation_chamber_revision": state.chamber_revision,
        }
        return _success(new_state, "item_used", reply)

    if item is ItemType.BURST:
        new_state = _consume_inventory(
            state,
            actor_seq,
            item,
            now=now,
            pending_burst=True,
        )
        return _success(new_state, "item_used", {"item": item.value})

    if item is ItemType.LOCK:
        target_seq = action.target_seq
        target = _seat_by_seq(state, target_seq)
        if target is None:
            return _failure(state, "player_seq_not_found")
        if target.join_seq == actor_seq:
            return _failure(state, "invalid_item_target", "self")
        if not target.alive:
            return _failure(state, "invalid_item_target", "eliminated")
        if target.join_seq in state.pending_locks:
            return _failure(
                state,
                "item_effect_conflict",
                "target_already_locked",
            )
        new_state = _consume_inventory(
            state,
            actor_seq,
            item,
            now=now,
            pending_locks=(*state.pending_locks, target.join_seq),
        )
        return _success(
            new_state,
            "item_used",
            {"item": item.value, "target_seq": target.join_seq},
        )

    # Beer discards exactly one round.  It does not make a live round lethal;
    # the ordinary shoot command is the lethal operation.
    chamber = list(state.ordered_chamber)
    chamber_revision = state.chamber_revision
    auto_reloaded = False
    reload_reason: str | None = None
    try:
        if not chamber:
            order = _random_chamber(random_source, INITIAL_LIVE, INITIAL_BLANK)
            if order is None:
                return _failure(state, "random_source_failed")
            chamber = list(order)
            chamber_revision += 1
            auto_reloaded = True
            reload_reason = "empty"
        consumed = chamber.pop(0)
        chamber_revision += 1
        reason = _normalization_reason(chamber)
        if reason is not None:
            order = _random_chamber(random_source, INITIAL_LIVE, INITIAL_BLANK)
            if order is None:
                return _failure(state, "random_source_failed")
            chamber = list(order)
            chamber_revision += 1
            auto_reloaded = True
            reload_reason = reason
    except Exception:
        return _failure(state, "random_source_failed")
    new_state = _consume_inventory(
        state,
        actor_seq,
        item,
        now=now,
        ordered_chamber=tuple(chamber),
        chamber_revision=chamber_revision,
    )
    return _success(
        new_state,
        "item_used",
        {
            "item": item.value,
            "consumed_kind": consumed.value,
            "remaining_live": chamber.count(ChamberKind.LIVE),
            "remaining_blank": chamber.count(ChamberKind.BLANK),
            "auto_reloaded": auto_reloaded,
            "reload_reason": reload_reason,
            "previous_chamber_revision": state.chamber_revision,
            "chamber_revision": chamber_revision,
        },
    )


def _discard_item(
    state: GameState,
    action: Action,
    actor_seq: int,
    now: datetime,
) -> ActionResult:
    if state.phase not in {"first_shot", "follow_up"}:
        return _failure(state, "action_not_allowed_in_phase")
    item = action.item
    actor = _seat_by_seq(state, actor_seq)
    if (
        not isinstance(item, ItemType)
        or actor is None
        or actor.inventory.get(item, 0) <= 0
    ):
        return _failure(state, "item_not_owned")
    new_state = _consume_inventory(
        state,
        actor_seq,
        item,
        now=now,
        pending_burst=state.pending_burst,
    )
    return _success(new_state, "item_discarded", {"item": item.value})


def _choose_item(
    state: GameState,
    action: Action,
    actor_seq: int,
    now: datetime,
) -> ActionResult:
    if state.phase != "item_choice" or not state.pending_rewards:
        return _failure(state, "action_not_allowed_in_phase")
    decision = action.decision
    first = state.pending_rewards[0]
    replacement: ItemType | None = None
    if decision == "replace":
        replacement = action.replace_item
        actor = _seat_by_seq(state, actor_seq)
        if not isinstance(replacement, ItemType) or actor is None:
            return _failure(state, "item_choice_invalid")
        if actor.inventory.get(replacement, 0) <= 0:
            return _failure(state, "item_not_owned")
    elif decision != "discard":
        return _failure(state, "item_choice_invalid")

    pending = state.pending_rewards[1:]
    new_state = state
    if decision == "replace":
        assert replacement is not None
        seats = _copy_seats(state.players)
        seat = _seat_index(seats, actor_seq)
        inventory = dict(seats[seat].inventory)
        inventory[replacement] -= 1
        if inventory[replacement] <= 0:
            del inventory[replacement]
        inventory[first] = inventory.get(first, 0) + 1
        seats[seat] = replace(seats[seat], inventory=inventory)
        new_state = replace(state, players=tuple(seats))
    new_state = replace(
        new_state,
        pending_rewards=tuple(pending),
        phase="item_choice" if pending else "follow_up",
        state_revision=state.state_revision + 1,
        deadline=now + TURN_DURATION,
    )
    reply: dict[str, Any] = {"decision": decision}
    if pending:
        reply.update(_item_choice_reply(new_state))
    else:
        reply["pending_item"] = None
    return _success(new_state, "item_choice_updated", reply)


def _reload(
    state: GameState,
    now: datetime,
    random_source: RandomSource,
) -> ActionResult:
    if state.phase != "follow_up":
        return _failure(state, "action_not_allowed_in_phase")
    if len(state.ordered_chamber) >= CHAMBER_SIZE:
        return _failure(state, "chamber_full")
    before_live = state.ordered_chamber.count(ChamberKind.LIVE)
    before_blank = state.ordered_chamber.count(ChamberKind.BLANK)
    live_count = before_live + 1
    blank_count = CHAMBER_SIZE - live_count
    chamber = _random_chamber(random_source, live_count, blank_count)
    if chamber is None:
        return _failure(state, "random_source_failed")
    new_state = replace(
        state,
        ordered_chamber=chamber,
        chamber_revision=state.chamber_revision + 1,
        state_revision=state.state_revision + 1,
        deadline=now + TURN_DURATION,
    )
    return _advance_turn_result(
        new_state,
        now,
        {
            "before_live": before_live,
            "before_blank": before_blank,
            "after_live": live_count,
            "after_blank": blank_count,
            "turn_ends": True,
            "information_invalidated": True,
            "chamber_revision": new_state.chamber_revision,
        },
        code="reloaded",
    )


def _end_turn(state: GameState, now: datetime) -> ActionResult:
    if state.phase != "follow_up":
        return _failure(state, "action_not_allowed_in_phase")
    new_state = replace(
        state,
        state_revision=state.state_revision + 1,
        deadline=now + TURN_DURATION,
    )
    return _advance_turn_result(new_state, now, {}, code="turn_ended")


def _panel(state: GameState, actor_seq: int) -> ActionResult:
    actor = _seat_by_seq(state, actor_seq)
    if actor is None:
        return _failure(state, "not_participant")
    inventory = {
        item.value: count for item, count in actor.inventory.items() if count > 0
    }
    return _success(state, "panel_opened", {"inventory": inventory})


def _forfeit(state: GameState, actor_seq: int, now: datetime) -> ActionResult:
    new_state = replace(
        state,
        pending_rewards=(),
        state_revision=state.state_revision + 1,
        deadline=now + TURN_DURATION,
    )
    return _eliminate_current(
        new_state,
        actor_seq,
        now,
        completion_reason="forfeit",
        reply={"completion_reason": "forfeit", "eliminated_reason": "forfeit"},
        code="forfeited",
    )


def _expire_command(state: GameState) -> ActionResult:
    if state.lifecycle == "active":
        return _failure(state, "turn_not_expired")
    if state.lifecycle == "waiting" and state.players:
        return _failure(state, "waiting_game_not_expired")
    return _failure(state, "no_active_game")


def _expire_waiting(state: GameState) -> ActionResult:
    new_state = replace(
        state,
        lifecycle="expired",
        phase=None,
        current_player_seq=None,
        deadline=None,
        state_revision=state.state_revision + 1,
        players=(),
        host_seq=None,
    )
    return _failure(new_state, "waiting_game_expired")


def _expire_active(state: GameState, now: datetime) -> ActionResult:
    current = state.current_player_seq
    if current is None:
        return _failure(state, "game_completed")
    # Expiry is a committed elimination, but the request itself is reported as
    # unsuccessful so callers do not treat it as a normal user action.
    new_state = replace(
        state,
        pending_rewards=(),
        state_revision=state.state_revision + 1,
        deadline=state.deadline + TURN_DURATION
        if state.deadline is not None
        else now + TURN_DURATION,
    )
    return _eliminate_current(
        new_state,
        current,
        now,
        completion_reason="timeout",
        reply={"eliminated_reason": "timeout"},
        code="turn_expired",
        ok=False,
    )


def _eliminate_current(
    state: GameState,
    actor_seq: int,
    now: datetime,
    *,
    completion_reason: str,
    reply: dict[str, Any],
    code: str = "shot",
    ok: bool = True,
) -> ActionResult:
    seats = _copy_seats(state.players)
    index = _seat_index(seats, actor_seq)
    seats[index] = replace(seats[index], alive=False)
    live_seqs = {seat.join_seq for seat in seats if seat.alive}
    locks = tuple(
        seq for seq in state.pending_locks if seq != actor_seq and seq in live_seqs
    )
    alive = [seat for seat in seats if seat.alive]
    if len(alive) <= 1:
        winner = alive[0].join_seq if alive else None
        terminal = replace(
            state,
            players=tuple(seats),
            lifecycle="completed",
            phase=None,
            current_player_seq=None,
            deadline=None,
            pending_rewards=(),
            pending_burst=False,
            pending_locks=(),
            turn_seq=state.turn_seq + 1,
        )
        terminal_reply = dict(reply)
        terminal_reply.update(
            completion_reason=completion_reason,
            winner_seq=winner,
        )
        return _result(terminal, ok=ok, code=code, reply=terminal_reply)
    next_seq = _next_alive_from(seats, actor_seq)
    if next_seq is None:
        return _failure(state, "no_survivor")
    next_locked = next_seq in locks
    if next_locked:
        locks = tuple(seq for seq in locks if seq != next_seq)
    updated = replace(
        state,
        players=tuple(seats),
        pending_locks=locks,
        current_player_seq=next_seq,
        phase="locked_turn" if next_locked else "first_shot",
        turn_seq=state.turn_seq + 1,
        deadline=state.deadline if not ok else now + TURN_DURATION,
    )
    return _result(updated, ok=ok, code=code, reply=reply)


def _advance_turn_result(
    state: GameState,
    now: datetime,
    reply: dict[str, Any],
    *,
    code: str,
) -> ActionResult:
    current = state.current_player_seq
    if current is None:
        return _failure(state, "no_current_player")
    next_seq = _next_alive_from(state.players, current)
    if next_seq is None:
        return _failure(state, "no_survivor")
    live_seqs = {seat.join_seq for seat in state.players if seat.alive}
    locks = tuple(seq for seq in state.pending_locks if seq in live_seqs)
    next_locked = next_seq in locks
    if next_locked:
        locks = tuple(seq for seq in locks if seq != next_seq)
    updated = replace(
        state,
        current_player_seq=next_seq,
        phase="locked_turn" if next_locked else "first_shot",
        pending_locks=locks,
        turn_seq=state.turn_seq + 1,
        deadline=now + TURN_DURATION,
    )
    return _success(updated, code, reply)


def _consume_inventory(
    state: GameState,
    actor_seq: int,
    item: ItemType,
    *,
    now: datetime,
    pending_burst: bool | None = None,
    pending_locks: tuple[int, ...] | None = None,
    ordered_chamber: tuple[ChamberKind, ...] | None = None,
    chamber_revision: int | None = None,
) -> GameState:
    seats = _copy_seats(state.players)
    index = _seat_index(seats, actor_seq)
    inventory = dict(seats[index].inventory)
    inventory[item] -= 1
    if inventory[item] <= 0:
        del inventory[item]
    seats[index] = replace(seats[index], inventory=inventory)
    return replace(
        state,
        players=tuple(seats),
        pending_burst=state.pending_burst if pending_burst is None else pending_burst,
        pending_locks=state.pending_locks if pending_locks is None else pending_locks,
        ordered_chamber=state.ordered_chamber
        if ordered_chamber is None
        else ordered_chamber,
        chamber_revision=state.chamber_revision
        if chamber_revision is None
        else chamber_revision,
        state_revision=state.state_revision + 1,
        deadline=now + TURN_DURATION,
    )


def _apply_rewards(
    state: GameState,
    rewards: Sequence[ItemType],
) -> tuple[GameState, tuple[ItemType, ...], bool]:
    seats = _copy_seats(state.players)
    index = _seat_index(seats, state.current_player_seq)
    inventory = dict(seats[index].inventory)
    pending = list(state.pending_rewards)
    visible: list[ItemType] = []
    for item in rewards:
        if sum(inventory.values()) < INVENTORY_CAPACITY and not pending:
            inventory[item] = inventory.get(item, 0) + 1
            visible.append(item)
        else:
            pending.append(item)
    seats[index] = replace(seats[index], inventory=inventory)
    return (
        replace(state, players=tuple(seats), pending_rewards=tuple(pending)),
        tuple(visible),
        bool(pending),
    )


def _item_choice_reply(state: GameState) -> dict[str, Any]:
    if not state.pending_rewards:
        return {"pending_item": None, "pending_item_count": 0}
    first = state.pending_rewards[0]
    count = 0
    for item in state.pending_rewards:
        if item is not first:
            break
        count += 1
    return {"pending_item": first.value, "pending_item_count": count}


def _is_expired(state: GameState, now: datetime) -> bool:
    return state.deadline is not None and now >= state.deadline


def _state_is_valid(state: GameState) -> bool:  # noqa: PLR0911
    """Check durable invariants before allowing a command to inspect details."""

    sequences = [seat.join_seq for seat in state.players]
    if len(sequences) != len(set(sequences)) or any(seq <= 0 for seq in sequences):
        return False
    if state.next_join_seq <= max(sequences, default=0):
        return False
    if state.lifecycle == "waiting":
        if len(state.players) > MAX_PLAYERS:
            return False
        if state.players and (
            state.host_seq is None
            or _seat_by_seq(state, state.host_seq) is None
            or state.deadline is None
        ):
            return False
        return not state.ordered_chamber and state.current_player_seq is None
    if state.lifecycle == "active":
        if len(state.players) < MIN_PLAYERS or len(state.players) > MAX_PLAYERS:
            return False
        if sum(seat.alive for seat in state.players) < MIN_PLAYERS:
            return False
        if state.phase not in {"first_shot", "follow_up", "locked_turn", "item_choice"}:
            return False
        current = _seat_by_seq(state, state.current_player_seq)
        if current is None or not current.alive or state.deadline is None:
            return False
        if state.pending_burst and len(state.ordered_chamber) < 2:
            return False
        if len(state.pending_locks) != len(set(state.pending_locks)):
            return False
        for target in state.pending_locks:
            seat = _seat_by_seq(state, target)
            if seat is None or not seat.alive:
                return False
        return True
    if state.lifecycle in {"completed", "cancelled", "expired"}:
        return state.current_player_seq is None
    return False


def _normalization_reason(chamber: Sequence[ChamberKind]) -> str | None:
    if not chamber:
        return "empty"
    if ChamberKind.LIVE not in chamber:
        return "no_live"
    return None


def _random_chamber(  # noqa: PLR0911
    random_source: RandomSource,
    live_count: int,
    blank_count: int,
) -> tuple[ChamberKind, ...] | None:
    try:
        value = random_source.chamber_order(live_count, blank_count)
    except Exception:
        return None
    if isinstance(value, (str, bytes, bytearray)):
        return None
    try:
        chamber = tuple(ChamberKind(item) for item in value)
    except (TypeError, ValueError):
        return None
    if len(chamber) != live_count + blank_count:
        return None
    if chamber.count(ChamberKind.LIVE) != live_count:
        return None
    if chamber.count(ChamberKind.BLANK) != blank_count:
        return None
    return chamber


def _random_item(
    random_source: RandomSource,
    weights: Mapping[ItemType, int],
) -> ItemType:
    value = random_source.weighted_item(dict(weights))
    return ItemType(value)


def _normalize_weights(
    weights: Mapping[ItemType, int] | None,
) -> dict[ItemType, int] | None:
    raw = DEFAULT_ITEM_WEIGHTS if weights is None else dict(weights)
    normalized: dict[ItemType, int] = {}
    for item in ITEM_TYPES:
        value = raw.get(item, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        normalized[item] = value
    if sum(normalized.values()) <= 0:
        return None
    return normalized


def _same_group(group: GroupRef, player: PlayerRef) -> bool:
    return group.app_id == player.app_id and group.group_openid == player.group_openid


def _find_player_seq(state: GameState, player: PlayerRef) -> int | None:
    if not _same_group(state.group, player):
        return None
    for seat in state.players:
        if seat.member_openid == player.member_openid:
            return seat.join_seq
    return None


def _seat_by_seq(state: GameState, join_seq: int | None) -> PlayerSeat | None:
    if join_seq is None:
        return None
    return next((seat for seat in state.players if seat.join_seq == join_seq), None)


def _copy_seats(players: Sequence[PlayerSeat]) -> list[PlayerSeat]:
    return [replace(seat, inventory=dict(seat.inventory)) for seat in players]


def _seat_index(players: Sequence[PlayerSeat], join_seq: int | None) -> int:
    if join_seq is None:
        raise ValueError("a current player is required")  # noqa: TRY003
    for index, seat in enumerate(players):
        if seat.join_seq == join_seq:
            return index
    raise ValueError("player sequence not found")  # noqa: TRY003


def _next_alive_from(players: Sequence[PlayerSeat], current_seq: int) -> int | None:
    if not players:
        return None
    try:
        start = _seat_index(players, current_seq)
    except ValueError:
        return next((seat.join_seq for seat in players if seat.alive), None)
    for offset in range(1, len(players) + 1):
        seat = players[(start + offset) % len(players)]
        if seat.alive:
            return seat.join_seq
    return None


def _success(
    state: GameState,
    code: str,
    reply: Mapping[str, Any] | None = None,
) -> ActionResult:
    return _result(state, ok=True, code=code, reply=dict(reply or {}))


def _failure(
    state: GameState,
    code: str,
    reason: str | None = None,
) -> ActionResult:
    return _result(state, ok=False, code=code, reason=reason, reply={})


def _result(
    state: GameState,
    *,
    ok: bool,
    code: str,
    reason: str | None = None,
    reply: Mapping[str, Any] | None = None,
) -> ActionResult:
    return ActionResult(ok, code, reason, state, dict(reply or {}))
