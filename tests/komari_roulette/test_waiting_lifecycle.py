"""TSK-260/TSK-268: waiting lifecycle, stable seats, and outer revisions."""

from __future__ import annotations

from datetime import timedelta

from tests.komari_roulette.support import (
    START,
    TURN,
    Action,
    ScriptedRandomSource,
    assert_ok,
    assert_rejected,
    create_waiting,
    dispatch,
    join,
    new_state,
    player,
    start_active,
)


def _seat(state: object, join_seq: int) -> object:
    return next(seat for seat in state.players if seat.join_seq == join_seq)  # type: ignore[attr-defined]


def _seat_seqs(state: object) -> list[int]:
    return [seat.join_seq for seat in state.players]  # type: ignore[attr-defined]


def test_waiting_join_seq_is_strictly_increasing_and_rejoin_does_not_reuse() -> None:
    state = create_waiting()
    assert state.lifecycle == "waiting"
    assert state.state_revision == 1
    assert _seat_seqs(state) == [1]

    for number in range(2, 7):
        result = join(state, number)
        assert_ok(result, "joined")
        state = result.state

    assert _seat_seqs(state) == [1, 2, 3, 4, 5, 6]
    full = join(state, 7)
    assert_rejected(full, "game_full")
    assert full.state == state

    left = dispatch(state, Action.leave(player(2)))
    assert_ok(left, "left")
    state = left.state
    assert _seat_seqs(state) == [1, 3, 4, 5, 6]

    rejoined = join(state, 2)
    assert_ok(rejoined, "joined")
    state = rejoined.state
    assert _seat_seqs(state) == [1, 3, 4, 5, 6, 7]
    assert _seat(state, 7).member_openid == "member-2"  # type: ignore[attr-defined]
    assert _seat(state, 7).display_name == "Player 2"  # type: ignore[attr-defined]


def test_host_leave_and_explicit_transfer_keep_waiting_order() -> None:
    state = create_waiting()
    joined = join(state, 2)
    assert_ok(joined, "joined")
    state = joined.state

    transferred = dispatch(state, Action.transfer(player(1), target_seq=2))
    assert_ok(transferred, "host_transferred")
    state = transferred.state
    assert state.host_seq == 2
    assert _seat_seqs(state) == [1, 2]

    rejected = dispatch(state, Action.cancel(player(1)))
    assert_rejected(rejected, "not_host")
    assert rejected.state == state

    host_left = dispatch(state, Action.leave(player(2)))
    assert_ok(host_left, "host_transferred")
    state = host_left.state
    assert state.lifecycle == "waiting"
    assert state.host_seq == 1
    assert _seat_seqs(state) == [1]


def test_transfer_rejects_stale_join_seq_after_member_rejoins() -> None:
    state = create_waiting()
    joined = join(state, 2)
    assert_ok(joined, "joined")
    left = dispatch(joined.state, Action.leave(player(2)))
    assert_ok(left, "left")
    rejoined = join(left.state, 2)
    assert_ok(rejoined, "joined")
    state = rejoined.state
    assert _seat_seqs(state) == [1, 3]

    stale = dispatch(state, Action.transfer(player(1), target_seq=2))
    assert_rejected(stale, "player_seq_not_found")
    assert stale.state == state

    current = dispatch(state, Action.transfer(player(1), target_seq=3))
    assert_ok(current, "host_transferred")
    assert current.state.host_seq == 3


def test_waiting_actions_have_stable_failures_and_random_start_is_atomic() -> None:
    state = create_waiting()
    duplicate = join(state, 1)
    assert_rejected(duplicate, "already_joined")
    assert duplicate.state == state

    too_few = dispatch(state, Action.start(player(1)))
    assert_rejected(too_few, "not_enough_players")
    assert too_few.state == state

    joined = join(state, 2)
    assert_ok(joined, "joined")
    state = joined.state
    not_host = dispatch(state, Action.start(player(2)))
    assert_rejected(not_host, "not_host")
    assert not_host.state == state

    entropy = ScriptedRandomSource()
    entropy.fail_next_chamber = True
    failed = dispatch(state, Action.start(player(1)), random_source=entropy)
    assert_rejected(failed, "random_source_failed")
    assert failed.state == state


def test_start_freezes_names_and_rejects_late_join() -> None:
    # Use the shared active fixture for a valid deterministic chamber.
    active, _ = start_active()
    assert active.lifecycle == "active"
    assert active.phase == "first_shot"
    assert active.turn_seq == 1
    assert active.chamber_revision == 1
    assert [seat.join_seq for seat in active.players] == [1, 2]
    assert [seat.display_name for seat in active.players] == [
        "Player 1",
        "Player 2",
    ]

    late = join(active, 3)
    assert_rejected(late, "game_already_started")
    assert late.state == active


def test_waiting_success_renews_but_duplicate_and_expiry_failure_do_not() -> None:
    state = create_waiting()
    initial_deadline = state.deadline
    assert initial_deadline == START + TURN

    joined = join(state, 2, now=START + timedelta(minutes=5))
    assert_ok(joined, "joined")
    state = joined.state
    renewed_deadline = state.deadline
    assert renewed_deadline == START + timedelta(minutes=20)

    duplicate = join(state, 2, now=START + timedelta(minutes=10))
    assert_rejected(duplicate, "already_joined")
    assert duplicate.state.deadline == renewed_deadline
    assert duplicate.state.state_revision == state.state_revision

    left = dispatch(
        state,
        Action.leave(player(2)),
        now=START + timedelta(minutes=10),
    )
    assert_ok(left, "left")
    assert left.state.deadline == START + timedelta(minutes=25)

    expired = dispatch(
        create_waiting(),
        Action.join(player(2)),
        now=START + TURN,
    )
    assert_rejected(expired, "waiting_game_expired")
    assert expired.state.lifecycle == "expired"


def test_outer_state_revision_and_chamber_revision_are_independent() -> None:
    state = create_waiting()
    joined = join(state, 2)
    assert_ok(joined, "joined")
    state = joined.state
    assert state.state_revision == 2
    assert state.chamber_revision == 0

    active, _ = start_active()
    assert active.state_revision == 3
    assert active.chamber_revision == 1

    panel = dispatch(active, Action.open_item_panel(player(1)))
    assert_ok(panel, "panel_opened")
    assert panel.state.state_revision == active.state_revision
    assert panel.state.chamber_revision == active.chamber_revision
    assert panel.state == active


def test_no_waiting_game_and_cancelled_game_are_distinct_from_active() -> None:
    absent = new_state()
    start = dispatch(absent, Action.start(player(1)))
    assert_rejected(start, "no_waiting_game")
    assert start.state == absent

    waiting = create_waiting()
    cancelled = dispatch(waiting, Action.cancel(player(1)))
    assert_ok(cancelled, "cancelled")
    assert cancelled.state.lifecycle == "cancelled"

    late_start = dispatch(cancelled.state, Action.start(player(1)))
    assert_rejected(late_start, "no_waiting_game")
