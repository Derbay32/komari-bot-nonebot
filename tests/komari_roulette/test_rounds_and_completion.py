"""TSK-262/TSK-268: turn permissions, timeout, forfeits, and one winner."""

from __future__ import annotations

from datetime import timedelta

from tests.komari_roulette.support import (
    START,
    TURN,
    Action,
    ChamberKind,
    ItemType,
    ScriptedRandomSource,
    assert_ok,
    assert_rejected,
    dispatch,
    player,
    public_facts,
    restore_trusted_state,
    scoped_player,
    start_active,
)


def test_only_current_surviving_participant_can_act() -> None:
    active, entropy = start_active()
    non_current = dispatch(active, Action.shoot(player(2)), random_source=entropy)
    assert_rejected(non_current, "not_current_player")
    assert non_current.state == active

    outsider = dispatch(active, Action.shoot(player(3)), random_source=entropy)
    assert_rejected(outsider, "not_participant")
    assert outsider.state == active


def test_same_member_openid_in_another_application_or_group_is_not_a_player() -> None:
    active, entropy = start_active()
    before_facts = public_facts(active)
    foreign_players = (
        scoped_player(1, app_id="other-app", group_openid="group-1"),
        scoped_player(1, app_id="qq", group_openid="other-group"),
    )

    for foreign in foreign_players:
        rejected = dispatch(
            active,
            Action.shoot(foreign),
            random_source=entropy,
        )
        assert_rejected(rejected, "not_participant")
        assert public_facts(active) == before_facts
        assert public_facts(rejected.state) == before_facts


def test_expired_current_request_returns_turn_expired_after_atomic_rotation() -> None:
    active, entropy = start_active(
        player_numbers=(1, 2, 3),
        chamber=(
            ChamberKind.BLANK,
            ChamberKind.BLANK,
            ChamberKind.LIVE,
            ChamberKind.BLANK,
            ChamberKind.LIVE,
            ChamberKind.BLANK,
        ),
    )
    before = active
    late = dispatch(
        before,
        Action.shoot(player(1)),
        now=START + TURN,
        random_source=entropy,
    )
    assert_rejected(late, "turn_expired")
    assert late.state != before
    assert late.state.players[0].alive is False
    assert late.state.current_player_seq == 2
    assert late.state.turn_seq == 2
    assert late.state.deadline == START + TURN + TURN
    assert late.reply["eliminated_reason"] == "timeout"


def test_forfeit_rotates_and_last_two_forfeit_completes_with_unique_winner() -> None:
    active, entropy = start_active(
        player_numbers=(1, 2, 3),
        chamber=(
            ChamberKind.BLANK,
            ChamberKind.BLANK,
            ChamberKind.LIVE,
            ChamberKind.BLANK,
            ChamberKind.LIVE,
            ChamberKind.BLANK,
        ),
    )
    first = dispatch(active, Action.forfeit(player(1)), random_source=entropy)
    assert_ok(first)
    assert first.state.lifecycle == "active"
    assert first.state.players[0].alive is False
    assert first.state.current_player_seq == 2

    second = dispatch(first.state, Action.forfeit(player(2)), random_source=entropy)
    assert_ok(second)
    assert second.state.lifecycle == "completed"
    assert second.reply["winner_seq"] == 3
    assert second.reply["completion_reason"] == "forfeit"
    assert second.state.current_player_seq is None
    assert second.state.turn_seq == first.state.turn_seq + 1

    late = dispatch(second.state, Action.shoot(player(3)), random_source=entropy)
    assert_rejected(late, "game_completed")
    assert late.state == second.state


def test_locked_blank_ends_the_turn_without_follow_up_or_reward() -> None:
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

    shot = dispatch(handed.state, Action.shoot(player(2)), random_source=entropy)
    assert_ok(shot, "shot")
    assert shot.reply["consumed_kind"] == "blank"
    assert shot.reply["reward_count"] == 0
    assert shot.state.phase == "first_shot"
    assert shot.state.current_player_seq == 3
    assert shot.state.players[1].alive is True


def test_follow_up_forfeit_rotates_and_drops_pending_rewards() -> None:
    before = restore_trusted_state(
        phase="follow_up",
        ordered_chamber=(
            ChamberKind.BLANK,
            ChamberKind.LIVE,
            ChamberKind.BLANK,
            ChamberKind.BLANK,
            ChamberKind.LIVE,
            ChamberKind.BLANK,
        ),
        pending_rewards=(ItemType.BEER, ItemType.MAGNIFIER),
    )
    forfeited = dispatch(before, Action.forfeit(player(1)))

    assert_ok(forfeited)
    assert forfeited.state.lifecycle == "active"
    assert forfeited.state.players[0].alive is False
    assert forfeited.state.current_player_seq == 2
    assert forfeited.state.pending_rewards == ()


def test_failed_and_read_only_actions_do_not_extend_the_turn_deadline() -> None:
    entropy = ScriptedRandomSource()
    active, _ = start_active(random_source=entropy)
    before_deadline = active.deadline

    failed = dispatch(
        active,
        Action.use_item(player(1), ItemType.BEER),
        now=START + timedelta(minutes=3),
        random_source=entropy,
    )
    assert_rejected(failed, "item_not_owned")
    assert failed.state == active
    assert failed.state.deadline == before_deadline

    panel = dispatch(
        active,
        Action.open_item_panel(player(1)),
        now=START + timedelta(minutes=3),
        random_source=entropy,
    )
    assert_ok(panel, "panel_opened")
    assert panel.state == active
    assert panel.state.deadline == before_deadline
