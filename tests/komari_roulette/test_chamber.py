"""TSK-261/TSK-268: ordered chamber, consumption, normalization, and reload."""

from __future__ import annotations

from tests.komari_roulette.support import (
    Action,
    ChamberKind,
    ItemType,
    ScriptedRandomSource,
    assert_no_secret_chamber_or_reward_fields,
    assert_ok,
    assert_rejected,
    dispatch,
    player,
    start_active,
)


def test_initial_chamber_is_ordered_but_shot_result_only_exposes_consumption() -> None:
    active, entropy = start_active(
        chamber=(
            ChamberKind.BLANK,
            ChamberKind.LIVE,
            ChamberKind.BLANK,
            ChamberKind.BLANK,
            ChamberKind.LIVE,
            ChamberKind.BLANK,
        )
    )
    assert entropy.chamber_calls == [(2, 4)]
    assert active.ordered_chamber == (
        ChamberKind.BLANK,
        ChamberKind.LIVE,
        ChamberKind.BLANK,
        ChamberKind.BLANK,
        ChamberKind.LIVE,
        ChamberKind.BLANK,
    )
    assert active.chamber_revision == 1

    shot = dispatch(active, Action.shoot(player(1)), random_source=entropy)
    assert_ok(shot, "shot")
    assert shot.reply["consumed_kind"] == "blank"
    assert shot.reply["remaining_live"] == 2
    assert shot.reply["remaining_blank"] == 3
    assert shot.reply["auto_reloaded"] is False
    assert shot.reply["reload_reason"] is None
    assert shot.reply["previous_chamber_revision"] == 1
    assert shot.reply["chamber_revision"] == 2
    assert shot.state.ordered_chamber == (
        ChamberKind.LIVE,
        ChamberKind.BLANK,
        ChamberKind.BLANK,
        ChamberKind.LIVE,
        ChamberKind.BLANK,
    )
    assert_no_secret_chamber_or_reward_fields(shot)


def test_live_shot_still_consumes_before_elimination_and_stops_the_action() -> None:
    active, entropy = start_active(
        chamber=(
            ChamberKind.BLANK,
            ChamberKind.LIVE,
            ChamberKind.BLANK,
            ChamberKind.BLANK,
            ChamberKind.LIVE,
            ChamberKind.BLANK,
        )
    )
    blank = dispatch(active, Action.shoot(player(1)), random_source=entropy)
    assert_ok(blank, "shot")

    live = dispatch(blank.state, Action.shoot(player(1)), random_source=entropy)
    assert_ok(live)
    assert live.reply["consumed_kind"] == "live"
    assert live.reply["auto_reloaded"] is False
    assert live.state.chamber_revision == 3
    assert live.state.lifecycle == "completed"
    assert_no_secret_chamber_or_reward_fields(live)


def test_no_live_normalizes_after_consumption_and_increments_chamber_revision_twice() -> (
    None
):
    entropy = ScriptedRandomSource(
        chambers=(
            (
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.LIVE,
                ChamberKind.LIVE,
                ChamberKind.BLANK,
            ),
            (
                ChamberKind.BLANK,
                ChamberKind.LIVE,
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.LIVE,
                ChamberKind.BLANK,
            ),
        ),
        items=(ItemType.BEER, ItemType.BEER),
    )
    active, _ = start_active(random_source=entropy)
    first = dispatch(active, Action.shoot(player(1)), random_source=entropy)
    assert_ok(first, "shot")
    second = dispatch(first.state, Action.shoot(player(1)), random_source=entropy)
    assert_ok(second, "shot")
    third = dispatch(second.state, Action.shoot(player(1)), random_source=entropy)
    assert_ok(third, "shot")
    before_beer = third.state
    assert before_beer.ordered_chamber == (
        ChamberKind.LIVE,
        ChamberKind.LIVE,
        ChamberKind.BLANK,
    )

    first_beer = dispatch(
        before_beer,
        Action.use_item(player(1), ItemType.BEER),
        random_source=entropy,
    )
    assert_ok(first_beer, "item_used")
    assert first_beer.reply["consumed_kind"] == "live"
    assert first_beer.reply["auto_reloaded"] is False
    assert first_beer.state.ordered_chamber == (
        ChamberKind.LIVE,
        ChamberKind.BLANK,
    )

    second_beer = dispatch(
        first_beer.state,
        Action.use_item(player(1), ItemType.BEER),
        random_source=entropy,
    )
    assert_ok(second_beer, "item_used")
    assert second_beer.reply["consumed_kind"] == "live"
    assert second_beer.reply["auto_reloaded"] is True
    assert second_beer.reply["reload_reason"] == "no_live"
    assert second_beer.reply["remaining_live"] == 2
    assert second_beer.reply["remaining_blank"] == 4
    assert (
        second_beer.reply["previous_chamber_revision"] + 2
        == (second_beer.reply["chamber_revision"])
    )
    assert second_beer.state.chamber_revision == before_beer.chamber_revision + 3
    assert_no_secret_chamber_or_reward_fields(second_beer)


def test_empty_has_priority_over_no_live_when_last_round_is_consumed() -> None:
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
                ChamberKind.LIVE,
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.LIVE,
                ChamberKind.BLANK,
            ),
        ),
        items=(ItemType.BEER, ItemType.BEER, ItemType.BEER),
    )
    active, _ = start_active(random_source=entropy)
    current = active
    for _ in range(4):
        shot = dispatch(current, Action.shoot(player(1)), random_source=entropy)
        assert_ok(shot, "shot")
        current = shot.state

    first_beer = dispatch(
        current,
        Action.use_item(player(1), ItemType.BEER),
        random_source=entropy,
    )
    assert_ok(first_beer, "item_used")
    second_beer = dispatch(
        first_beer.state,
        Action.use_item(player(1), ItemType.BEER),
        random_source=entropy,
    )
    assert_ok(second_beer, "item_used")
    assert second_beer.reply["reload_reason"] == "empty"
    assert second_beer.reply["reload_reason"] != "no_live"
    assert second_beer.reply["remaining_live"] == 2
    assert second_beer.reply["remaining_blank"] == 4


def test_manual_reload_preserves_remaining_live_and_adds_exactly_one_live() -> None:
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
            (
                ChamberKind.LIVE,
                ChamberKind.LIVE,
                ChamberKind.LIVE,
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.BLANK,
            ),
        ),
        items=(ItemType.MAGNIFIER,),
    )
    active, _ = start_active(random_source=entropy)
    first = dispatch(active, Action.shoot(player(1)), random_source=entropy)
    assert_ok(first, "shot")
    second = dispatch(first.state, Action.shoot(player(1)), random_source=entropy)
    assert_ok(second, "shot")
    assert second.state.ordered_chamber == (
        ChamberKind.LIVE,
        ChamberKind.BLANK,
        ChamberKind.LIVE,
        ChamberKind.BLANK,
    )

    reload = dispatch(
        second.state,
        Action.reload(player(1)),
        random_source=entropy,
    )
    assert_ok(reload, "reloaded")
    assert reload.reply["before_live"] == 2
    assert reload.reply["before_blank"] == 2
    assert reload.reply["after_live"] == 3
    assert reload.reply["after_blank"] == 3
    assert reload.reply["turn_ends"] is True
    assert reload.reply["information_invalidated"] is True
    assert reload.state.chamber_revision == second.state.chamber_revision + 1
    assert reload.state.turn_seq == 2
    assert reload.state.current_player_seq == 2
    assert reload.state.phase == "first_shot"
    assert reload.state.ordered_chamber == (
        ChamberKind.LIVE,
        ChamberKind.BLANK,
        ChamberKind.LIVE,
        ChamberKind.BLANK,
        ChamberKind.BLANK,
        ChamberKind.BLANK,
    )


def test_first_shot_cannot_manually_reload_or_end_turn() -> None:
    active, entropy = start_active()
    reload = dispatch(active, Action.reload(player(1)), random_source=entropy)
    assert_rejected(reload, "action_not_allowed_in_phase")
    end_turn = dispatch(active, Action.end_turn(player(1)), random_source=entropy)
    assert_rejected(end_turn, "action_not_allowed_in_phase")
    assert reload.state == active
    assert end_turn.state == active


def test_random_failure_during_auto_reload_leaves_the_whole_action_uncommitted() -> (
    None
):
    entropy = ScriptedRandomSource(
        chambers=(
            (
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.LIVE,
                ChamberKind.LIVE,
                ChamberKind.BLANK,
            ),
        ),
        items=(ItemType.BEER, ItemType.BEER),
    )
    active, _ = start_active(random_source=entropy)
    current = active
    for _ in range(3):
        shot = dispatch(current, Action.shoot(player(1)), random_source=entropy)
        assert_ok(shot, "shot")
        current = shot.state
    beer = dispatch(
        current, Action.use_item(player(1), ItemType.BEER), random_source=entropy
    )
    assert_ok(beer, "item_used")
    before_failure = beer.state
    entropy.fail_next_chamber = True
    failed = dispatch(
        before_failure,
        Action.use_item(player(1), ItemType.BEER),
        random_source=entropy,
    )
    assert_rejected(failed, "random_source_failed")
    assert failed.state == before_failure
