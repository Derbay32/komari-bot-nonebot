"""TSK-275 persistence mapper contract tests."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from komari_bot.plugins.komari_roulette.mapper import (
    GameSnapshot,
    game_state_from_snapshot,
    game_state_to_snapshot,
)

from komari_bot.plugins.komari_roulette.domain import ItemType
from tests.komari_roulette.storage_support import (
    START,
    active_state,
    active_state_with_join_gap,
    group_for,
    player_member_ids,
    scope,
)


def test_mapper_round_trip_preserves_all_restart_facts() -> None:
    """Restart recovery keeps queues, frozen names, weights, and next sequence."""

    app_id, group_openid = scope("mapper")
    group = group_for(app_id, group_openid)
    state = active_state(
        group,
        player_count=2,
        names=("冻结甲", "冻结乙"),
        pending_rewards=(ItemType.BEER, ItemType.LOCK),
        pending_locks=(2,),
    )
    game_id = str(uuid4())

    snapshot = game_state_to_snapshot(state, game_id=game_id)
    restored = game_state_from_snapshot(snapshot)

    assert isinstance(snapshot, GameSnapshot)
    assert snapshot.game_id == game_id
    assert restored.group == group
    assert restored.lifecycle == "active"
    assert restored.phase == state.phase == "item_choice"
    assert restored.state_revision == state.state_revision
    assert restored.chamber_revision == state.chamber_revision
    assert restored.turn_seq == state.turn_seq
    assert restored.current_player_seq == state.current_player_seq
    assert restored.deadline == state.deadline
    assert restored.host_seq == state.host_seq
    assert restored.next_join_seq == state.next_join_seq
    assert restored.ordered_chamber == state.ordered_chamber
    assert restored.pending_rewards == (ItemType.BEER, ItemType.LOCK)
    assert restored.pending_locks == (2,)
    assert dict(restored.item_weights) == {
        ItemType.MAGNIFIER: 4,
        ItemType.BEER: 3,
        ItemType.BURST: 2,
        ItemType.LOCK: 1,
    }
    assert player_member_ids(restored) == player_member_ids(state)
    assert [seat.display_name for seat in restored.players] == [
        "冻结甲",
        "冻结乙",
    ]


def test_mapper_copies_input_and_returns_immutable_collections() -> None:
    """A snapshot cannot be mutated through caller-owned mappings or lists."""

    app_id, group_openid = scope("mapper-copy")
    group = group_for(app_id, group_openid)
    state = active_state(group)
    snapshot = game_state_to_snapshot(state, game_id=str(uuid4()))

    restored = game_state_from_snapshot(snapshot)
    with pytest.raises(TypeError):
        restored.item_weights[ItemType.BEER] = 99  # type: ignore[index]
    with pytest.raises(AttributeError):
        restored.players.append(object())  # type: ignore[attr-defined]

    assert restored.deadline == START + timedelta(minutes=15)


def test_mapper_preserves_non_reused_join_sequence_above_six() -> None:
    app_id, group_openid = scope("mapper-gap")
    state = active_state_with_join_gap(group_for(app_id, group_openid))
    snapshot = game_state_to_snapshot(state, game_id=str(uuid4()))
    restored = game_state_from_snapshot(snapshot)

    assert [seat.join_seq for seat in restored.players] == [1, 6, 7]
    assert restored.next_join_seq == 8
    assert [seat.display_name for seat in restored.players] == [
        "Player 1",
        "Player 6",
        "Player 2",
    ]
