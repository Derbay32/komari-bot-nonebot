"""Typed persistence DTOs and the trusted state conversion boundary.

The domain state intentionally contains only current facts.  This module adds
the durable identity and transition metadata needed by PostgreSQL while
keeping all collections immutable and validating rows before they re-enter the
state machine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, cast

from .domain import (
    CHAMBER_SIZE,
    DEFAULT_ITEM_WEIGHTS,
    INVENTORY_CAPACITY,
    MAX_PLAYERS,
    MIN_PLAYERS,
    ActionResult,
    ChamberKind,
    GameState,
    GroupRef,
    ItemType,
    PlayerRef,
    PlayerSeat,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import MappingProxyType


LIFECYCLES = frozenset(
    {"waiting", "active", "completed", "cancelled", "expired", "failed"}
)
ACTIVE_PHASES = frozenset({"first_shot", "follow_up", "locked_turn", "item_choice"})
TERMINAL_LIFECYCLES = frozenset({"completed", "cancelled", "expired", "failed"})


@dataclass(frozen=True, slots=True)
class GameSnapshot:
    """A database identity paired with an immutable trusted game state."""

    game_id: str
    state: GameState
    created_at: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    waiting_expires_at: datetime | None = None
    turn_deadline_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.game_id, str) or not self.game_id.strip():
            raise ValueError("game_id must not be empty")  # noqa: TRY003
        if not isinstance(self.state, GameState):
            raise TypeError("snapshot state must be a GameState")  # noqa: TRY003
        _validate_state(self.state)
        for value in (
            self.created_at,
            self.started_at,
            self.ended_at,
            self.waiting_expires_at,
            self.turn_deadline_at,
        ):
            _validate_timestamp(value)

    @classmethod
    def from_state(cls, state: GameState, *, game_id: str) -> GameSnapshot:
        """Create a storage identity for a trusted domain state."""

        return cls(game_id=game_id, state=_copy_state(state))

    # The flattened properties keep the public seam convenient for callers
    # that need a durable field without exposing ORM rows.
    @property
    def group(self) -> GroupRef:
        return self.state.group

    @property
    def lifecycle(self) -> str:
        return self.state.lifecycle

    @property
    def phase(self) -> str | None:
        return self.state.phase

    @property
    def state_revision(self) -> int:
        return self.state.state_revision

    @property
    def chamber_revision(self) -> int:
        return self.state.chamber_revision

    @property
    def turn_seq(self) -> int:
        return self.state.turn_seq

    @property
    def current_player_seq(self) -> int | None:
        return self.state.current_player_seq

    @property
    def deadline(self) -> datetime | None:
        return self.state.deadline

    @property
    def host_seq(self) -> int | None:
        return self.state.host_seq

    @property
    def players(self) -> tuple[PlayerSeat, ...]:
        return self.state.players

    @property
    def ordered_chamber(self) -> tuple[ChamberKind, ...]:
        return self.state.ordered_chamber

    @property
    def pending_rewards(self) -> tuple[ItemType, ...]:
        return self.state.pending_rewards

    @property
    def pending_burst(self) -> bool:
        return self.state.pending_burst

    @property
    def pending_locks(self) -> tuple[int, ...]:
        return self.state.pending_locks

    @property
    def item_weights(self) -> MappingProxyType[ItemType, int]:
        return cast("MappingProxyType[ItemType, int]", self.state.item_weights)

    @property
    def next_join_seq(self) -> int:
        return self.state.next_join_seq


@dataclass(frozen=True, slots=True)
class EliminationRecord:
    """One committed transition's newly eliminated seat."""

    join_seq: int
    reason: str
    occurred_at: datetime

    def __post_init__(self) -> None:
        if self.join_seq <= 0 or not self.reason.strip():
            raise ValueError("invalid elimination record")  # noqa: TRY003
        _validate_timestamp(self.occurred_at)


@dataclass(frozen=True, slots=True)
class StateTransition:
    """A caller-owned state transition and its durable history delta."""

    before: GameSnapshot
    after: GameSnapshot
    action_kind: str
    occurred_at: datetime
    eliminations: tuple[EliminationRecord, ...] = ()

    def __post_init__(self) -> None:
        if self.before.game_id != self.after.game_id:
            raise ValueError("a transition must keep its game identity")  # noqa: TRY003
        if not self.action_kind.strip():
            raise ValueError("action_kind must not be empty")  # noqa: TRY003
        _validate_timestamp(self.occurred_at)
        object.__setattr__(self, "eliminations", tuple(self.eliminations))


@dataclass(frozen=True, slots=True)
class TerminalProjection:
    """The caller's terminal intent, verified again by the storage adapter."""

    snapshot: GameSnapshot
    lifecycle: str
    reason: str
    ended_at: datetime
    winner_seq: int | None

    def __post_init__(self) -> None:
        if self.lifecycle not in TERMINAL_LIFECYCLES:
            raise ValueError("terminal lifecycle is not closed")  # noqa: TRY003
        if not self.reason.strip():
            raise ValueError("terminal reason must not be empty")  # noqa: TRY003
        _validate_timestamp(self.ended_at)
        if self.winner_seq is not None and self.winner_seq <= 0:
            raise ValueError("winner sequence must be positive")  # noqa: TRY003

    @classmethod
    def from_state(
        cls,
        snapshot: GameSnapshot,
        *,
        lifecycle: str,
        reason: str,
        ended_at: datetime,
        winner_seq: int | None,
    ) -> TerminalProjection:
        return cls(
            snapshot=snapshot,
            lifecycle=lifecycle,
            reason=reason,
            ended_at=ended_at,
            winner_seq=winner_seq,
        )

    @property
    def game_id(self) -> str:
        return self.snapshot.game_id

    @property
    def group(self) -> GroupRef:
        return self.snapshot.group


@dataclass(frozen=True, slots=True)
class ResultPlayer:
    """Immutable result seat with its frozen display name."""

    join_seq: int
    member_openid: str
    display_name: str
    alive: bool
    eliminated_order: int | None
    eliminated_reason: str | None
    eliminated_at: datetime | None


@dataclass(frozen=True, slots=True)
class RouletteResult:
    """Immutable terminal result returned by ``get_result``."""

    game_id: str
    app_id: str
    group_openid: str
    lifecycle: str
    reason: str
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime
    terminal_revision: int
    winner_seq: int | None
    winner_member_openid: str | None
    winner_display_name: str | None
    players: tuple[ResultPlayer, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "players", tuple(self.players))


@dataclass(frozen=True, slots=True)
class LeaderboardEntry:
    """Safe display projection; member openids remain storage-internal."""

    display_name: str
    wins: int
    last_won_at: datetime


def game_state_to_snapshot(state: GameState, *, game_id: str) -> GameSnapshot:
    """Copy a trusted state into an immutable storage DTO."""

    return GameSnapshot.from_state(state, game_id=game_id)


def game_state_from_snapshot(snapshot: GameSnapshot) -> GameState:
    """Rebuild and fully validate a state from the persistence seam."""

    if not isinstance(snapshot, GameSnapshot):
        raise TypeError("expected GameSnapshot")  # noqa: TRY003
    return _copy_state(snapshot.state)


def transition_from_action_result(
    before: GameSnapshot,
    result: ActionResult,
    *,
    action_kind: str,
    occurred_at: datetime,
) -> StateTransition:
    """Build the only public transition DTO from a domain action result."""

    after = game_state_to_snapshot(result.state, game_id=before.game_id)
    before_by_seq = {seat.join_seq: seat for seat in before.players}
    after_by_seq = {seat.join_seq: seat for seat in after.players}
    reason = _elimination_reason(action_kind, result)
    eliminations = tuple(
        EliminationRecord(seq, reason, occurred_at)
        for seq, old in before_by_seq.items()
        if old.alive and seq in after_by_seq and not after_by_seq[seq].alive
    )
    return StateTransition(
        before=before,
        after=after,
        action_kind=action_kind,
        occurred_at=occurred_at,
        eliminations=eliminations,
    )


def _elimination_reason(action_kind: str, result: ActionResult) -> str:
    reply_reason = result.reply.get("eliminated_reason")
    if isinstance(reply_reason, str) and reply_reason.strip():
        return reply_reason
    completion_reason = result.reply.get("completion_reason")
    if isinstance(completion_reason, str) and completion_reason.strip():
        return completion_reason
    return {
        "shoot": "shot",
        "forfeit": "forfeit",
        "expire": "timeout",
    }.get(action_kind, action_kind)


def _copy_state(state: GameState) -> GameState:
    """Reconstruct typed values instead of trusting a copying factory."""

    _validate_state(state)
    players = tuple(
        PlayerSeat(
            player=PlayerRef(
                app_id=seat.player.app_id,
                group_openid=seat.player.group_openid,
                member_openid=seat.player.member_openid,
                display_name=seat.player.display_name,
            ),
            join_seq=seat.join_seq,
            alive=seat.alive,
            inventory={ItemType(item): value for item, value in seat.inventory.items()},
        )
        for seat in state.players
    )
    copied = GameState(
        group=GroupRef(state.group.app_id, state.group.group_openid),
        lifecycle=state.lifecycle,
        phase=state.phase,
        state_revision=state.state_revision,
        chamber_revision=state.chamber_revision,
        turn_seq=state.turn_seq,
        current_player_seq=state.current_player_seq,
        deadline=state.deadline,
        host_seq=state.host_seq,
        players=players,
        ordered_chamber=tuple(ChamberKind(item) for item in state.ordered_chamber),
        pending_rewards=tuple(ItemType(item) for item in state.pending_rewards),
        pending_burst=state.pending_burst,
        pending_locks=tuple(state.pending_locks),
        item_weights={
            ItemType(item): value for item, value in state.item_weights.items()
        },
        next_join_seq=state.next_join_seq,
    )
    _validate_state(copied)
    return copied


def _validate_state(state: GameState) -> None:
    """Validate all invariants needed before a state may be persisted/resumed."""

    if type(state.lifecycle) is not str or state.lifecycle not in LIFECYCLES:
        raise ValueError("unknown game lifecycle")  # noqa: TRY003
    if state.phase is not None and type(state.phase) is not str:
        raise TypeError("phase is invalid")  # noqa: TRY003
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in (state.state_revision, state.chamber_revision, state.turn_seq)
    ):
        raise ValueError("negative state revision")  # noqa: TRY003
    _validate_timestamp(state.deadline)
    if not isinstance(state.group, GroupRef):
        raise TypeError("invalid group identity")  # noqa: TRY003
    for seat in state.players:
        if not isinstance(seat, PlayerSeat):
            raise TypeError("invalid player seat")  # noqa: TRY003
        if type(seat.join_seq) is not int or type(seat.alive) is not bool:
            raise TypeError("player seat scalar is invalid")  # noqa: TRY003
    for value in (state.current_player_seq, state.host_seq):
        if value is not None and (type(value) is not int or value <= 0):
            raise TypeError("player sequence scalar is invalid")  # noqa: TRY003
    if type(state.next_join_seq) is not int or state.next_join_seq <= 0:
        raise ValueError("next join sequence is invalid")  # noqa: TRY003
    if type(state.pending_burst) is not bool:
        raise TypeError("pending burst flag is invalid")  # noqa: TRY003
    sequences = [seat.join_seq for seat in state.players]
    if sequences != sorted(sequences) or len(set(sequences)) != len(sequences):
        raise ValueError("player order or join sequence is invalid")  # noqa: TRY003
    if any(seq <= 0 for seq in sequences) or state.next_join_seq <= max(
        sequences, default=0
    ):
        raise ValueError("join sequence invariant is invalid")  # noqa: TRY003
    if len(state.players) > MAX_PLAYERS:
        raise ValueError("too many players")  # noqa: TRY003
    member_ids = [seat.member_openid for seat in state.players]
    display_names = [seat.display_name for seat in state.players]
    if len(member_ids) != len(set(member_ids)) or len(display_names) != len(
        set(display_names)
    ):
        raise ValueError("frozen player identity or name is duplicated")  # noqa: TRY003
    for seat in state.players:
        if (
            seat.player.app_id != state.group.app_id
            or seat.player.group_openid != state.group.group_openid
        ):
            raise ValueError("player group does not match game group")  # noqa: TRY003
        _validate_inventory(seat.inventory)
    if set(state.item_weights) != set(DEFAULT_ITEM_WEIGHTS) or any(
        not isinstance(item, ItemType) for item in state.item_weights
    ):
        raise ValueError("item weights are incomplete")  # noqa: TRY003
    if (
        any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in state.item_weights.values()
        )
        or sum(state.item_weights.values()) <= 0
    ):
        raise ValueError("item weights are invalid")  # noqa: TRY003
    if len(state.ordered_chamber) > CHAMBER_SIZE or any(
        not isinstance(item, ChamberKind) for item in state.ordered_chamber
    ):
        raise ValueError("ordered chamber is invalid")  # noqa: TRY003
    if len(state.pending_rewards) > INVENTORY_CAPACITY or any(
        not isinstance(item, ItemType) for item in state.pending_rewards
    ):
        raise ValueError("pending rewards are invalid")  # noqa: TRY003
    if len(set(state.pending_locks)) != len(state.pending_locks) or any(
        isinstance(item, bool) or not isinstance(item, int) or item <= 0
        for item in state.pending_locks
    ):
        raise ValueError("pending locks are invalid")  # noqa: TRY003
    seats_by_seq = {seat.join_seq: seat for seat in state.players}
    if any(
        target not in seats_by_seq or not seats_by_seq[target].alive
        for target in state.pending_locks
    ):
        raise ValueError("pending lock target is invalid")  # noqa: TRY003

    match state.lifecycle:
        case "waiting":
            if state.phase is not None or state.current_player_seq is not None:
                raise ValueError("waiting state has active fields")  # noqa: TRY003
            if (
                state.ordered_chamber
                or state.pending_rewards
                or state.pending_locks
                or state.pending_burst
            ):
                raise ValueError("waiting state contains runtime secrets")  # noqa: TRY003
            if state.players:
                if state.host_seq not in seats_by_seq or state.deadline is None:
                    raise ValueError("waiting state has no host or deadline")  # noqa: TRY003
            elif state.host_seq is not None or state.deadline is not None:
                raise ValueError("empty waiting state has host or deadline")  # noqa: TRY003
        case "active":
            if not MIN_PLAYERS <= len(state.players) <= MAX_PLAYERS:
                raise ValueError("active roster size is invalid")  # noqa: TRY003
            if sum(seat.alive for seat in state.players) < MIN_PLAYERS:
                raise ValueError("active roster has too few survivors")  # noqa: TRY003
            if state.phase not in ACTIVE_PHASES or state.deadline is None:
                raise ValueError("active phase or deadline is invalid")  # noqa: TRY003
            if not 0 < len(state.ordered_chamber) <= CHAMBER_SIZE or (
                ChamberKind.LIVE not in state.ordered_chamber
            ):
                raise ValueError("active chamber has no live round")  # noqa: TRY003
            if state.chamber_revision <= 0 or state.turn_seq <= 0:
                raise ValueError("active revisions are invalid")  # noqa: TRY003
            current_seq = state.current_player_seq
            current = seats_by_seq.get(current_seq) if current_seq is not None else None
            if current is None or not current.alive:
                raise ValueError("active current player is invalid")  # noqa: TRY003
            if state.pending_burst and len(state.ordered_chamber) < 2:
                raise ValueError("burst requires two chamber entries")  # noqa: TRY003
            if state.phase == "item_choice" and not state.pending_rewards:
                raise ValueError("item choice has no pending reward")  # noqa: TRY003
            if state.phase != "item_choice" and state.pending_rewards:
                raise ValueError("pending reward outside item choice")  # noqa: TRY003
            if (
                state.phase == "item_choice"
                and sum(current.inventory.values()) < INVENTORY_CAPACITY
            ):
                raise ValueError("item choice requires a full inventory")  # noqa: TRY003
            if state.phase == "locked_turn" and current.join_seq in state.pending_locks:
                raise ValueError("current locked player remains pending")  # noqa: TRY003
        case "completed":
            if (
                state.phase is not None
                or state.deadline is not None
                or state.current_player_seq is not None
                or sum(seat.alive for seat in state.players) != 1
            ):
                raise ValueError("completed state must have one winner")  # noqa: TRY003
        case "cancelled" | "expired" | "failed":
            if (
                state.phase is not None
                or state.deadline is not None
                or state.current_player_seq is not None
            ):
                raise ValueError("terminal state has a current player")  # noqa: TRY003


def _validate_inventory(inventory: Mapping[ItemType, int]) -> None:
    if any(
        not isinstance(item, ItemType)
        or isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        for item, value in inventory.items()
    ):
        raise ValueError("inventory value is invalid")  # noqa: TRY003
    if sum(inventory.values()) > INVENTORY_CAPACITY:
        raise ValueError("inventory capacity exceeded")  # noqa: TRY003


def _validate_timestamp(value: datetime | None) -> None:
    if value is not None and (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("timestamps must be timezone-aware")  # noqa: TRY003


__all__ = [
    "EliminationRecord",
    "GameSnapshot",
    "LeaderboardEntry",
    "ResultPlayer",
    "RouletteResult",
    "StateTransition",
    "TerminalProjection",
    "game_state_from_snapshot",
    "game_state_to_snapshot",
    "transition_from_action_result",
]
