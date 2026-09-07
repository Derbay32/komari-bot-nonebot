"""TSK-262/TSK-263/TSK-268: phases, inventory, effects, and reward secrecy."""

from __future__ import annotations

from datetime import timedelta

from tests.komari_roulette.support import (
    START,
    Action,
    ChamberKind,
    ItemType,
    ScriptedRandomSource,
    assert_no_secret_chamber_or_reward_fields,
    assert_ok,
    assert_rejected,
    dispatch,
    player,
    public_facts,
    restore_trusted_state,
    start_active,
)


def _seat(state: object, join_seq: int) -> object:
    return next(seat for seat in state.players if seat.join_seq == join_seq)  # type: ignore[attr-defined]


def _inventory(state: object, join_seq: int) -> dict[ItemType, int]:
    return dict(_seat(state, join_seq).inventory)  # type: ignore[attr-defined]


def test_first_shot_all_blank_enters_follow_up_without_reward() -> None:
    entropy = ScriptedRandomSource(
        chambers=(
            (
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.LIVE,
                ChamberKind.LIVE,
                ChamberKind.BLANK,
                ChamberKind.BLANK,
            ),
        ),
        items=(ItemType.BEER,),
    )
    active, _ = start_active(random_source=entropy)
    result = dispatch(active, Action.shoot(player(1)), random_source=entropy)
    assert_ok(result, "shot")
    assert result.state.phase == "follow_up"
    assert _inventory(result.state, 1) == {}
    assert entropy.item_calls == []
    assert result.reply["reward_count"] == 0


def test_follow_up_all_blank_draws_one_reward_per_blank_in_order() -> None:
    entropy = ScriptedRandomSource(
        chambers=(
            (
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.LIVE,
                ChamberKind.LIVE,
            ),
        ),
        items=(ItemType.BEER, ItemType.MAGNIFIER, ItemType.LOCK),
    )
    active, _ = start_active(random_source=entropy)
    first = dispatch(active, Action.shoot(player(1)), random_source=entropy)
    assert_ok(first, "shot")
    second = dispatch(first.state, Action.shoot(player(1)), random_source=entropy)
    assert_ok(second, "shot")
    third = dispatch(second.state, Action.shoot(player(1)), random_source=entropy)
    assert_ok(third, "shot")
    assert _inventory(third.state, 1) == {
        ItemType.BEER: 1,
        ItemType.MAGNIFIER: 1,
    }
    assert list(entropy.item_calls) == [
        {
            ItemType.MAGNIFIER: 1,
            ItemType.BEER: 1,
            ItemType.BURST: 1,
            ItemType.LOCK: 1,
        },
        {
            ItemType.MAGNIFIER: 1,
            ItemType.BEER: 1,
            ItemType.BURST: 1,
            ItemType.LOCK: 1,
        },
    ]
    assert third.reply["reward_count"] == 1
    assert third.reply["rewards"] == [ItemType.MAGNIFIER.value]


def test_start_freezes_item_weights_before_later_reward_draws() -> None:
    weights = {
        ItemType.MAGNIFIER: 1,
        ItemType.BEER: 2,
        ItemType.BURST: 3,
        ItemType.LOCK: 4,
    }
    entropy = ScriptedRandomSource(
        chambers=(
            (
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.LIVE,
                ChamberKind.LIVE,
                ChamberKind.BLANK,
                ChamberKind.BLANK,
            ),
        ),
        items=(ItemType.BEER,),
    )
    active, _ = start_active(random_source=entropy, item_weights=weights)
    expected = dict(weights)
    weights[ItemType.BEER] = 99

    first = dispatch(active, Action.shoot(player(1)), random_source=entropy)
    assert_ok(first, "shot")
    second = dispatch(first.state, Action.shoot(player(1)), random_source=entropy)
    assert_ok(second, "shot")
    assert entropy.item_calls == [expected]


def test_magnifier_consumes_inventory_and_renews_without_chamber_mutation() -> None:
    entropy = ScriptedRandomSource(
        chambers=(
            (
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.LIVE,
                ChamberKind.LIVE,
                ChamberKind.BLANK,
                ChamberKind.BLANK,
            ),
        ),
        items=(ItemType.MAGNIFIER,),
    )
    active, _ = start_active(random_source=entropy)
    first = dispatch(active, Action.shoot(player(1)), random_source=entropy)
    assert_ok(first, "shot")
    reward = dispatch(first.state, Action.shoot(player(1)), random_source=entropy)
    assert_ok(reward, "shot")
    before = reward.state
    observed = dispatch(
        before,
        Action.use_item(player(1), ItemType.MAGNIFIER),
        now=START + timedelta(minutes=1),
        random_source=entropy,
    )
    assert_ok(observed, "item_used")
    assert observed.reply["observed_kind"] == "live"
    assert observed.reply["observation_chamber_revision"] == before.chamber_revision
    assert observed.state.chamber_revision == before.chamber_revision
    assert observed.state.state_revision == before.state_revision + 1
    assert observed.state.deadline == START + timedelta(minutes=16)
    assert _inventory(observed.state, 1) == {}
    assert_no_secret_chamber_or_reward_fields(observed)


def test_first_shot_allows_an_ordinary_item_without_changing_the_phase() -> None:
    before = restore_trusted_state(
        phase="first_shot",
        ordered_chamber=(ChamberKind.LIVE,),
        inventory={ItemType.MAGNIFIER: 1},
    )
    observed = dispatch(
        before,
        Action.use_item(player(1), ItemType.MAGNIFIER),
        now=START + timedelta(minutes=1),
    )

    assert_ok(observed, "item_used")
    assert observed.state.phase == "first_shot"
    assert observed.state.current_player_seq == before.current_player_seq
    assert observed.state.chamber_revision == before.chamber_revision
    assert observed.state.state_revision == before.state_revision + 1
    assert observed.state.deadline == START + timedelta(minutes=16)
    assert observed.reply["observed_kind"] == "live"


def test_burst_requires_at_least_two_remaining_rounds() -> None:
    before = restore_trusted_state(
        phase="follow_up",
        ordered_chamber=(ChamberKind.LIVE,),
        inventory={ItemType.BURST: 1},
    )
    rejected = dispatch(
        before,
        Action.use_item(player(1), ItemType.BURST),
    )

    assert_rejected(rejected, "item_precondition_failed", "burst_requires_two_rounds")
    assert rejected.state == before


def test_discard_item_is_allowed_in_follow_up_and_renews_the_turn() -> None:
    entropy = ScriptedRandomSource(
        chambers=(
            (
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.LIVE,
                ChamberKind.BLANK,
                ChamberKind.LIVE,
                ChamberKind.BLANK,
            ),
        ),
        items=(ItemType.MAGNIFIER,),
    )
    active, _ = start_active(random_source=entropy)
    first = dispatch(active, Action.shoot(player(1)), random_source=entropy)
    assert_ok(first, "shot")
    reward = dispatch(first.state, Action.shoot(player(1)), random_source=entropy)
    assert_ok(reward, "shot")
    discarded = dispatch(
        reward.state,
        Action.discard_item(player(1), ItemType.MAGNIFIER),
        now=START + timedelta(minutes=1),
        random_source=entropy,
    )
    assert_ok(discarded, "item_discarded")
    assert _inventory(discarded.state, 1) == {}
    assert discarded.state.phase == "follow_up"
    assert discarded.state.deadline == START + timedelta(minutes=16)


def test_discard_item_is_also_allowed_in_first_shot_after_handoff() -> None:
    entropy = ScriptedRandomSource(
        chambers=(
            (
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.LIVE,
                ChamberKind.LIVE,
            ),
        ),
        items=(ItemType.MAGNIFIER,),
    )
    active, _ = start_active(
        player_numbers=(1, 2, 3),
        random_source=entropy,
    )
    first = dispatch(active, Action.shoot(player(1)), random_source=entropy)
    assert_ok(first, "shot")
    reward = dispatch(first.state, Action.shoot(player(1)), random_source=entropy)
    assert_ok(reward, "shot")
    p2_turn = dispatch(reward.state, Action.end_turn(player(1)), random_source=entropy)
    assert_ok(p2_turn, "turn_ended")
    p2_follow = dispatch(p2_turn.state, Action.shoot(player(2)), random_source=entropy)
    assert_ok(p2_follow, "shot")
    p3_turn = dispatch(
        p2_follow.state, Action.end_turn(player(2)), random_source=entropy
    )
    assert_ok(p3_turn, "turn_ended")
    p3_follow = dispatch(p3_turn.state, Action.shoot(player(3)), random_source=entropy)
    assert_ok(p3_follow, "shot")
    p1_turn = dispatch(
        p3_follow.state, Action.end_turn(player(3)), random_source=entropy
    )
    assert_ok(p1_turn, "turn_ended")
    assert p1_turn.state.phase == "first_shot"
    assert p1_turn.state.current_player_seq == 1

    discarded = dispatch(
        p1_turn.state,
        Action.discard_item(player(1), ItemType.MAGNIFIER),
        now=START + timedelta(minutes=1),
        random_source=entropy,
    )
    assert_ok(discarded, "item_discarded")
    assert _inventory(discarded.state, 1) == {}
    assert discarded.state.phase == "first_shot"


def test_locked_turn_rejects_items_reload_discard_panel_and_end_turn() -> None:
    entropy = ScriptedRandomSource(
        chambers=(
            (
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.LIVE,
                ChamberKind.BLANK,
                ChamberKind.LIVE,
                ChamberKind.BLANK,
            ),
        ),
        items=(ItemType.LOCK,),
    )
    active, _ = start_active(
        player_numbers=(1, 2, 3),
        random_source=entropy,
    )
    first = dispatch(active, Action.shoot(player(1)), random_source=entropy)
    assert_ok(first, "shot")
    reward = dispatch(first.state, Action.shoot(player(1)), random_source=entropy)
    assert_ok(reward, "shot")
    locked = dispatch(
        reward.state,
        Action.use_item(player(1), ItemType.LOCK, target_seq=2),
        random_source=entropy,
    )
    assert_ok(locked, "item_used")
    handed = dispatch(locked.state, Action.end_turn(player(1)), random_source=entropy)
    assert_ok(handed, "turn_ended")
    assert handed.state.phase == "locked_turn"
    assert handed.state.current_player_seq == 2

    for action in (
        Action.use_item(player(2), ItemType.MAGNIFIER),
        Action.discard_item(player(2), ItemType.LOCK),
        Action.reload(player(2)),
        Action.open_item_panel(player(2)),
        Action.end_turn(player(2)),
    ):
        rejected = dispatch(handed.state, action, random_source=entropy)
        assert_rejected(rejected, "locked_turn_restriction")
        assert rejected.state == handed.state

    forfeited = dispatch(
        handed.state,
        Action.forfeit(player(2)),
        random_source=entropy,
    )
    assert_ok(forfeited)
    assert forfeited.state.players[1].alive is False
    assert forfeited.state.current_player_seq == 3


def test_lock_binds_to_next_turn_and_is_cleared_when_target_is_eliminated() -> None:
    entropy = ScriptedRandomSource(
        chambers=(
            (
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.LIVE,
                ChamberKind.LIVE,
                ChamberKind.BLANK,
                ChamberKind.BLANK,
            ),
        ),
        items=(ItemType.LOCK,),
    )
    active, _ = start_active(
        player_numbers=(1, 2, 3),
        random_source=entropy,
    )
    first = dispatch(active, Action.shoot(player(1)), random_source=entropy)
    assert_ok(first, "shot")
    reward = dispatch(first.state, Action.shoot(player(1)), random_source=entropy)
    assert_ok(reward, "shot")
    self_target = dispatch(
        reward.state,
        Action.use_item(player(1), ItemType.LOCK, target_seq=1),
        random_source=entropy,
    )
    assert_rejected(self_target, "invalid_item_target", "self")
    assert self_target.state == reward.state

    locked = dispatch(
        reward.state,
        Action.use_item(player(1), ItemType.LOCK, target_seq=2),
        random_source=entropy,
    )
    assert_ok(locked, "item_used")
    issuer_dies = dispatch(
        locked.state,
        Action.shoot(player(1)),
        random_source=entropy,
    )
    assert_ok(issuer_dies, "shot")
    assert issuer_dies.state.current_player_seq == 2
    assert issuer_dies.state.phase == "locked_turn"
    assert not issuer_dies.state.pending_locks

    target_dies = dispatch(
        issuer_dies.state,
        Action.shoot(player(2)),
        random_source=entropy,
    )
    assert_ok(target_dies, "shot")
    assert target_dies.state.players[1].alive is False
    assert not target_dies.state.pending_locks
    assert target_dies.state.current_player_seq == 3


def test_lock_rejects_missing_dead_and_duplicate_targets_without_consuming() -> None:
    common = {
        "phase": "follow_up",
        "ordered_chamber": (
            ChamberKind.BLANK,
            ChamberKind.LIVE,
            ChamberKind.BLANK,
            ChamberKind.BLANK,
            ChamberKind.LIVE,
            ChamberKind.BLANK,
        ),
        "inventory": {ItemType.LOCK: 1},
        "player_numbers": (1, 2, 3),
    }
    missing = restore_trusted_state(**common)
    missing_facts = public_facts(missing)
    missing_result = dispatch(
        missing,
        Action.use_item(player(1), ItemType.LOCK, target_seq=99),
    )
    assert_rejected(missing_result, "invalid_item_target", "not_found")
    assert public_facts(missing) == missing_facts
    assert public_facts(missing_result.state) == missing_facts

    dead = restore_trusted_state(**common, dead_player_numbers=(2,))
    dead_facts = public_facts(dead)
    dead_result = dispatch(
        dead,
        Action.use_item(player(1), ItemType.LOCK, target_seq=2),
    )
    assert_rejected(dead_result, "invalid_item_target", "not_alive")
    assert public_facts(dead) == dead_facts
    assert public_facts(dead_result.state) == dead_facts

    duplicate = restore_trusted_state(**common, pending_locks=(2,))
    duplicate_facts = public_facts(duplicate)
    duplicate_result = dispatch(
        duplicate,
        Action.use_item(player(1), ItemType.LOCK, target_seq=2),
    )
    assert_rejected(
        duplicate_result,
        "item_precondition_failed",
        "target_already_locked",
    )
    assert public_facts(duplicate) == duplicate_facts
    assert public_facts(duplicate_result.state) == duplicate_facts


def test_burst_survives_handoff_and_first_live_stops_the_second_consumption() -> None:
    entropy = ScriptedRandomSource(
        chambers=(
            (
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.LIVE,
                ChamberKind.LIVE,
            ),
        ),
        items=(ItemType.BEER, ItemType.BURST),
    )
    active, _ = start_active(random_source=entropy)
    first = dispatch(active, Action.shoot(player(1)), random_source=entropy)
    assert_ok(first, "shot")
    beer_reward = dispatch(first.state, Action.shoot(player(1)), random_source=entropy)
    assert_ok(beer_reward, "shot")
    burst_reward = dispatch(
        beer_reward.state,
        Action.shoot(player(1)),
        random_source=entropy,
    )
    assert_ok(burst_reward, "shot")
    armed = dispatch(
        burst_reward.state,
        Action.use_item(player(1), ItemType.BURST),
        random_source=entropy,
    )
    assert_ok(armed, "item_used")
    assert armed.state.pending_burst is True

    beer_conflict = dispatch(
        armed.state,
        Action.use_item(player(1), ItemType.BEER),
        random_source=entropy,
    )
    assert_rejected(beer_conflict, "item_precondition_failed", "beer_blocked_by_burst")
    assert beer_conflict.state == armed.state

    handed = dispatch(armed.state, Action.end_turn(player(1)), random_source=entropy)
    assert_ok(handed, "turn_ended")
    assert handed.state.pending_burst is True
    assert handed.state.current_player_seq == 2

    burst = dispatch(handed.state, Action.shoot(player(2)), random_source=entropy)
    assert_ok(burst, "shot")
    assert burst.reply["consumptions"] == ["blank", "live"]
    assert burst.reply["reward_count"] == 0
    assert burst.state.pending_burst is False
    assert burst.state.players[1].alive is False
    assert burst.state.lifecycle == "active"
    assert burst.state.current_player_seq == 1


def test_burst_survives_reload_forfeit_and_timeout() -> None:
    before = restore_trusted_state(
        phase="follow_up",
        ordered_chamber=(ChamberKind.LIVE, ChamberKind.BLANK),
        pending_burst=True,
        inventory={},
    )

    reloaded = dispatch(
        before,
        Action.reload(player(1)),
        random_source=ScriptedRandomSource(
            chambers=(
                (
                    ChamberKind.LIVE,
                    ChamberKind.LIVE,
                    ChamberKind.BLANK,
                    ChamberKind.BLANK,
                    ChamberKind.BLANK,
                    ChamberKind.BLANK,
                ),
            )
        ),
    )
    assert_ok(reloaded, "reloaded")
    assert reloaded.state.pending_burst is True

    forfeited = dispatch(before, Action.forfeit(player(1)))
    assert_ok(forfeited)
    assert forfeited.state.pending_burst is True
    assert forfeited.state.current_player_seq == 2

    timed_out = dispatch(
        before,
        Action.expire(),
        now=START + timedelta(minutes=15),
    )
    assert_rejected(timed_out, "turn_expired")
    assert timed_out.state.pending_burst is True
    assert timed_out.state.current_player_seq == 2


def test_item_choice_is_the_only_action_allowed_after_full_inventory_reward() -> None:
    entropy = ScriptedRandomSource(
        chambers=(
            (
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.LIVE,
                ChamberKind.LIVE,
            ),
            (
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.LIVE,
                ChamberKind.LIVE,
            ),
            (
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.LIVE,
                ChamberKind.LIVE,
            ),
        ),
        items=(
            ItemType.BEER,
            ItemType.BEER,
            ItemType.BURST,
            ItemType.MAGNIFIER,
            ItemType.LOCK,
            ItemType.BEER,
            ItemType.BEER,
            ItemType.BEER,
            ItemType.BURST,
            ItemType.LOCK,
            ItemType.BEER,
        ),
    )
    active, _ = start_active(random_source=entropy)
    current = active
    for _ in range(4):
        result = dispatch(current, Action.shoot(player(1)), random_source=entropy)
        assert_ok(result, "shot")
        current = result.state

    first_beer = dispatch(
        current, Action.use_item(player(1), ItemType.BEER), random_source=entropy
    )
    assert_ok(first_beer, "item_used")
    second_beer = dispatch(
        first_beer.state,
        Action.use_item(player(1), ItemType.BEER),
        random_source=entropy,
    )
    assert_ok(second_beer, "item_used")
    current = second_beer.state

    armed_after_reload = dispatch(
        current,
        Action.use_item(player(1), ItemType.BURST),
        random_source=entropy,
    )
    assert_ok(armed_after_reload, "item_used")
    burst_shot = dispatch(
        armed_after_reload.state,
        Action.shoot(player(1)),
        random_source=entropy,
    )
    assert_ok(burst_shot, "shot")
    first_after_burst = dispatch(
        burst_shot.state,
        Action.shoot(player(1)),
        random_source=entropy,
    )
    assert_ok(first_after_burst, "shot")
    second_after_burst = dispatch(
        first_after_burst.state,
        Action.shoot(player(1)),
        random_source=entropy,
    )
    assert_ok(second_after_burst, "shot")
    first_beer_after_burst = dispatch(
        second_after_burst.state,
        Action.use_item(player(1), ItemType.BEER),
        random_source=entropy,
    )
    assert_ok(first_beer_after_burst, "item_used")
    second_beer_after_burst = dispatch(
        first_beer_after_burst.state,
        Action.use_item(player(1), ItemType.BEER),
        random_source=entropy,
    )
    assert_ok(second_beer_after_burst, "item_used")
    first_o3 = dispatch(
        second_beer_after_burst.state,
        Action.shoot(player(1)),
        random_source=entropy,
    )
    assert_ok(first_o3, "shot")
    second_o3 = dispatch(
        first_o3.state,
        Action.shoot(player(1)),
        random_source=entropy,
    )
    assert_ok(second_o3, "shot")
    armed = dispatch(
        second_o3.state,
        Action.use_item(player(1), ItemType.BURST),
        random_source=entropy,
    )
    assert_ok(armed, "item_used")
    item_choice = dispatch(armed.state, Action.shoot(player(1)), random_source=entropy)
    assert_ok(item_choice, "item_choice_pending")
    assert item_choice.state.phase == "item_choice"
    assert item_choice.reply["pending_item"] == ItemType.BEER.value
    assert item_choice.reply["pending_item_count"] == 1
    assert_no_secret_chamber_or_reward_fields(item_choice)

    forfeited = dispatch(
        item_choice.state,
        Action.forfeit(player(1)),
        random_source=entropy,
    )
    assert_ok(forfeited)
    assert forfeited.state.lifecycle == "completed"
    assert forfeited.reply["completion_reason"] == "forfeit"
    assert not forfeited.state.pending_rewards

    for action in (
        Action.shoot(player(1)),
        Action.use_item(player(1), ItemType.MAGNIFIER),
        Action.discard_item(player(1), ItemType.BEER),
        Action.reload(player(1)),
        Action.open_item_panel(player(1)),
        Action.end_turn(player(1)),
    ):
        rejected = dispatch(item_choice.state, action, random_source=entropy)
        assert_rejected(rejected, "action_not_allowed_in_phase", "item_choice_pending")
        assert rejected.state == item_choice.state

    replaced = dispatch(
        item_choice.state,
        Action.choose_item(
            player(1),
            decision="replace",
            replace_item=ItemType.BEER,
        ),
        random_source=entropy,
    )
    assert_ok(replaced, "item_choice_updated")
    assert replaced.state.phase == "follow_up"
    assert _inventory(replaced.state, 1) == _inventory(item_choice.state, 1)


def test_random_failure_in_pre_draw_rolls_back_shot_chamber_rewards_and_deadline() -> (
    None
):
    entropy = ScriptedRandomSource(
        chambers=(
            (
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.LIVE,
                ChamberKind.LIVE,
            ),
        ),
        items=(ItemType.BURST,),
    )
    active, _ = start_active(random_source=entropy)
    first = dispatch(active, Action.shoot(player(1)), random_source=entropy)
    assert_ok(first, "shot")
    reward = dispatch(first.state, Action.shoot(player(1)), random_source=entropy)
    assert_ok(reward, "shot")
    armed = dispatch(
        reward.state,
        Action.use_item(player(1), ItemType.BURST),
        random_source=entropy,
    )
    assert_ok(armed, "item_used")
    before = armed.state
    entropy.fail_next_item = True
    failed = dispatch(before, Action.shoot(player(1)), random_source=entropy)
    assert_rejected(failed, "random_source_failed")
    assert failed.state == before
    assert failed.state.state_revision == before.state_revision
    assert failed.state.chamber_revision == before.chamber_revision
    assert failed.state.deadline == before.deadline


def test_item_choice_safe_reply_hides_future_reward_types() -> None:
    first = restore_trusted_state(
        phase="follow_up",
        ordered_chamber=(
            ChamberKind.BLANK,
            ChamberKind.BLANK,
            ChamberKind.LIVE,
            ChamberKind.LIVE,
            ChamberKind.BLANK,
            ChamberKind.BLANK,
        ),
        pending_burst=True,
    )
    second = restore_trusted_state(
        phase="follow_up",
        ordered_chamber=first.ordered_chamber,
        pending_burst=True,
    )
    first_entropy = ScriptedRandomSource(items=(ItemType.BEER, ItemType.MAGNIFIER))
    second_entropy = ScriptedRandomSource(items=(ItemType.BEER, ItemType.LOCK))

    first_result = dispatch(
        first,
        Action.shoot(player(1)),
        random_source=first_entropy,
    )
    second_result = dispatch(
        second,
        Action.shoot(player(1)),
        random_source=second_entropy,
    )
    assert_ok(first_result, "item_choice_pending")
    assert_ok(second_result, "item_choice_pending")
    assert (
        first_result.state.pending_rewards[0] == second_result.state.pending_rewards[0]
    )
    assert (
        len(first_result.state.pending_rewards)
        == len(second_result.state.pending_rewards)
        == 2
    )
    assert (
        first_result.state.pending_rewards[1] != second_result.state.pending_rewards[1]
    )
    assert first_result.reply == second_result.reply
    assert first_result.reply["pending_item"] == ItemType.BEER.value
    assert (
        first_result.reply["pending_item_count"]
        == second_result.reply["pending_item_count"]
    )
    assert_no_secret_chamber_or_reward_fields(first_result)
    assert_no_secret_chamber_or_reward_fields(second_result)


def test_item_choice_discards_multiple_rewards_one_at_a_time_and_renews_each() -> None:
    before = restore_trusted_state(
        phase="follow_up",
        ordered_chamber=(
            ChamberKind.BLANK,
            ChamberKind.BLANK,
            ChamberKind.LIVE,
            ChamberKind.LIVE,
            ChamberKind.BLANK,
            ChamberKind.BLANK,
        ),
        pending_burst=True,
    )
    original_inventory = _inventory(before, 1)
    entropy = ScriptedRandomSource(items=(ItemType.BEER, ItemType.MAGNIFIER))
    pending = dispatch(before, Action.shoot(player(1)), random_source=entropy)
    assert_ok(pending, "item_choice_pending")
    assert pending.state.phase == "item_choice"
    assert pending.state.pending_rewards == (ItemType.BEER, ItemType.MAGNIFIER)

    failed = dispatch(
        pending.state,
        Action.choose_item(player(1), decision="replace"),
        now=START + timedelta(minutes=3),
    )
    assert not failed.ok
    assert public_facts(failed.state) == public_facts(pending.state)
    assert failed.state.deadline == pending.state.deadline

    first_discard = dispatch(
        pending.state,
        Action.choose_item(player(1), decision="discard"),
        now=START + timedelta(minutes=1),
    )
    assert_ok(first_discard)
    assert first_discard.state.phase == "item_choice"
    assert first_discard.state.pending_rewards == (ItemType.MAGNIFIER,)
    assert _inventory(first_discard.state, 1) == original_inventory
    assert first_discard.state.deadline == START + timedelta(minutes=16)
    assert first_discard.state.state_revision == pending.state.state_revision + 1

    second_discard = dispatch(
        first_discard.state,
        Action.choose_item(player(1), decision="discard"),
        now=START + timedelta(minutes=2),
    )
    assert_ok(second_discard)
    assert second_discard.state.phase == "follow_up"
    assert second_discard.state.pending_rewards == ()
    assert _inventory(second_discard.state, 1) == original_inventory
    assert second_discard.state.deadline == START + timedelta(minutes=17)
    assert second_discard.state.state_revision == first_discard.state.state_revision + 1


def test_item_choice_timeout_drops_unprocessed_rewards_before_rotation() -> None:
    before = restore_trusted_state(
        phase="item_choice",
        ordered_chamber=(
            ChamberKind.BLANK,
            ChamberKind.LIVE,
            ChamberKind.BLANK,
            ChamberKind.BLANK,
            ChamberKind.LIVE,
            ChamberKind.BLANK,
        ),
        pending_rewards=(ItemType.BEER, ItemType.MAGNIFIER),
        player_numbers=(1, 2, 3),
    )

    timed_out = dispatch(
        before,
        Action.expire(),
        now=START + timedelta(minutes=15),
    )

    assert_rejected(timed_out, "turn_expired")
    assert timed_out.state.lifecycle == "active"
    assert timed_out.state.players[0].alive is False
    assert timed_out.state.current_player_seq == 2
    assert timed_out.state.pending_rewards == ()
    assert timed_out.reply["eliminated_reason"] == "timeout"


def test_second_reward_draw_failure_rolls_back_the_whole_pre_draw() -> None:
    before = restore_trusted_state(
        phase="follow_up",
        ordered_chamber=(
            ChamberKind.BLANK,
            ChamberKind.BLANK,
            ChamberKind.LIVE,
            ChamberKind.LIVE,
            ChamberKind.BLANK,
            ChamberKind.BLANK,
        ),
        pending_burst=True,
        inventory={},
    )
    entropy = ScriptedRandomSource(items=(ItemType.BEER,))
    entropy.fail_on_item_call = 2
    before_facts = public_facts(before)

    failed = dispatch(
        before,
        Action.shoot(player(1)),
        random_source=entropy,
    )

    assert_rejected(failed, "random_source_failed")
    assert len(entropy.item_calls) == 2
    assert public_facts(before) == before_facts
    assert public_facts(failed.state) == before_facts
