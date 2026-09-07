"""Public-domain test helpers for TSK-272.

The helpers deliberately speak only the proposed domain seam.  They do not
reach into a repository, a NoneBot event, a binding manager, or a sender.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from komari_bot.plugins.komari_roulette.domain import (
    Action,
    ActionResult,
    ChamberKind,
    GameState,
    GroupRef,
    ItemType,
    PlayerRef,
    apply_action,
    initial_state,
)

APP_ID = "qq"
GROUP_OPENID = "group-1"
GROUP = GroupRef(app_id=APP_ID, group_openid=GROUP_OPENID)
START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
TURN = timedelta(minutes=15)


def player(number: int, *, name: str | None = None) -> PlayerRef:
    """Build a protocol-scoped participant with a frozen display name."""

    return scoped_player(
        number,
        app_id=APP_ID,
        group_openid=GROUP_OPENID,
        name=name,
    )


def scoped_player(
    number: int,
    *,
    app_id: str,
    group_openid: str,
    name: str | None = None,
) -> PlayerRef:
    """Build a participant whose application/group scope is explicit."""

    return PlayerRef(
        app_id=app_id,
        group_openid=group_openid,
        member_openid=f"member-{number}",
        display_name=name or f"Player {number}",
    )


def ordered_chamber(*kinds: ChamberKind) -> tuple[ChamberKind, ...]:
    """Make an explicit deterministic chamber order for a test."""

    return tuple(kinds)


class ScriptedRandomSource:
    """Inject complete chamber orders and weighted item choices.

    The production seam may use another implementation, but it must accept
    these two operations or an equivalent structural protocol.  The fake
    intentionally never exposes a random seed or samples in the test.
    """

    def __init__(
        self,
        *,
        chambers: Iterable[Sequence[ChamberKind]] = (),
        items: Iterable[ItemType] = (),
    ) -> None:
        self._chambers = deque(tuple(order) for order in chambers)
        self._items = deque(items)
        self.fail_next_chamber = False
        self.fail_next_item = False
        self.fail_on_item_call: int | None = None
        self.chamber_calls: list[tuple[int, int]] = []
        self.item_calls: list[Mapping[ItemType, int]] = []

    def chamber_order(
        self,
        live_count: int,
        blank_count: int,
    ) -> tuple[ChamberKind, ...]:
        self.chamber_calls.append((live_count, blank_count))
        if self.fail_next_chamber:
            self.fail_next_chamber = False
            raise RuntimeError
        if not self._chambers:
            raise AssertionError
        order = self._chambers.popleft()
        assert len(order) == live_count + blank_count
        return order

    def weighted_item(self, weights: Mapping[ItemType, int]) -> ItemType:
        self.item_calls.append(dict(weights))
        if self.fail_next_item:
            self.fail_next_item = False
            raise RuntimeError
        if self.fail_on_item_call == len(self.item_calls):
            self.fail_on_item_call = None
            raise RuntimeError
        if not self._items:
            raise AssertionError
        item = self._items.popleft()
        assert weights[item] > 0
        return item


def dispatch(
    state: GameState,
    action: Action,
    *,
    now: datetime = START,
    random_source: ScriptedRandomSource | None = None,
) -> ActionResult:
    """Apply one public command at an injected time and random source."""

    return apply_action(
        state,
        action,
        now=now,
        random_source=random_source or ScriptedRandomSource(),
    )


def new_state() -> GameState:
    return initial_state(GROUP)


def create_waiting(
    *, host: int = 1, random_source: ScriptedRandomSource | None = None
) -> GameState:
    result = dispatch(
        new_state(),
        Action.create(player(host)),
        random_source=random_source,
    )
    assert_ok(result, "created")
    return result.state


def join(
    state: GameState,
    number: int,
    *,
    now: datetime = START,
    random_source: ScriptedRandomSource | None = None,
) -> ActionResult:
    return dispatch(
        state,
        Action.join(player(number)),
        now=now,
        random_source=random_source,
    )


def start_active(
    *,
    player_numbers: Sequence[int] = (1, 2),
    chamber: Sequence[ChamberKind] | None = None,
    random_source: ScriptedRandomSource | None = None,
    item_weights: Mapping[ItemType, int] | None = None,
) -> tuple[GameState, ScriptedRandomSource]:
    entropy = random_source or ScriptedRandomSource(
        chambers=(
            chamber
            or (
                ChamberKind.BLANK,
                ChamberKind.LIVE,
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.LIVE,
                ChamberKind.BLANK,
            ),
        )
    )
    state = create_waiting(host=player_numbers[0], random_source=entropy)
    for number in player_numbers[1:]:
        joined = join(state, number, random_source=entropy)
        assert_ok(joined, "joined")
        state = joined.state
    start_action = (
        Action.start(player(player_numbers[0]), item_weights=item_weights)
        if item_weights is not None
        else Action.start(player(player_numbers[0]))
    )
    started = dispatch(state, start_action, random_source=entropy)
    assert_ok(started, "started")
    return started.state, entropy


def restore_trusted_state(
    *,
    phase: str,
    ordered_chamber: Sequence[ChamberKind],
    pending_rewards: Sequence[ItemType] = (),
    pending_burst: bool = False,
    inventory: Mapping[ItemType, int] | None = None,
    player_numbers: Sequence[int] = (1, 2),
    dead_player_numbers: Sequence[int] = (),
    pending_locks: Sequence[int] = (),
) -> GameState:
    """Build a valid storage-recovery snapshot through the public domain seam.

    This keeps item-choice and rollback tests focused on their Given state.  The
    mapping is trusted persistence input, not a user reply or an ORM object.
    """

    player_inventory = dict(
        {
            ItemType.BEER: 1,
            ItemType.MAGNIFIER: 1,
            ItemType.BURST: 1,
            ItemType.LOCK: 1,
        }
        if inventory is None
        else inventory
    )
    players = tuple(
        {
            "player": player(number),
            "join_seq": join_seq,
            "alive": number not in dead_player_numbers,
            "inventory": player_inventory if join_seq == 1 else {},
        }
        for join_seq, number in enumerate(player_numbers, start=1)
    )
    return GameState.from_trusted_snapshot(
        {
            "group": GROUP,
            "lifecycle": "active",
            "phase": phase,
            "state_revision": 12,
            "chamber_revision": 4,
            "turn_seq": 3,
            "current_player_seq": 1,
            "deadline": START + TURN,
            "host_seq": 1,
            "players": players,
            "ordered_chamber": tuple(ordered_chamber),
            "pending_rewards": tuple(pending_rewards),
            "pending_burst": pending_burst,
            "pending_locks": tuple(pending_locks),
            "item_weights": {
                ItemType.MAGNIFIER: 1,
                ItemType.BEER: 1,
                ItemType.BURST: 1,
                ItemType.LOCK: 1,
            },
        }
    )


def public_facts(state: GameState) -> tuple[Any, ...]:
    """Snapshot public durable facts without relying on object identity."""

    players = tuple(
        (
            seat.join_seq,
            seat.member_openid,
            seat.display_name,
            seat.alive,
            dict(seat.inventory),
        )
        for seat in state.players
    )
    return (
        state.lifecycle,
        state.phase,
        state.state_revision,
        state.chamber_revision,
        state.turn_seq,
        state.current_player_seq,
        state.deadline,
        state.host_seq,
        deepcopy(state.ordered_chamber),
        deepcopy(state.pending_rewards),
        deepcopy(state.pending_burst),
        deepcopy(state.pending_locks),
        players,
    )


def assert_ok(result: ActionResult, code: str | None = None) -> None:
    assert result.ok, (result.code, result.reason, result.reply)
    if code is not None:
        assert result.code == code


def assert_rejected(result: ActionResult, code: str, reason: str | None = None) -> None:
    assert not result.ok
    assert result.code == code
    if reason is not None:
        assert result.reason == reason


def public_payload(result: ActionResult) -> Mapping[str, Any]:
    """Return the safe result mapping without peeking into trusted state."""

    payload = result.reply
    assert isinstance(payload, Mapping)
    return payload


def flatten_keys(value: object) -> set[str]:
    """Collect mapping keys recursively to detect accidental secret leakage."""

    if isinstance(value, Mapping):
        keys = {str(key) for key in value}
        for child in value.values():
            keys.update(flatten_keys(child))
        return keys
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        keys: set[str] = set()
        for child in value:
            keys.update(flatten_keys(child))
        return keys
    return set()


def assert_no_secret_chamber_or_reward_fields(result: ActionResult) -> None:
    forbidden = {
        "ordered_chamber",
        "chamber_order",
        "next_chamber",
        "current_round",
        "pending_rewards",
        "future_rewards",
        "random_seed",
        "rng_state",
    }
    assert not (flatten_keys(public_payload(result)) & forbidden)


def require_attr(obj: object, name: str) -> Any:
    """Use an explicitly public contract field and fail clearly if absent."""

    return getattr(obj, name)


@pytest.fixture
def entropy() -> ScriptedRandomSource:
    return ScriptedRandomSource()
