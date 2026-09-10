"""TSK-278 domain regressions for confirmed production gaps.

These tests speak only the public domain seam (``tests/komari_roulette.support``):
no repository, no NoneBot event, no binding manager, no sender.  Each test name
states the confirmed TSK-266 decision it guards so a failure points at exactly
one acceptance criterion.
"""

from __future__ import annotations

from tests.komari_roulette.support import (
    Action,
    assert_ok,
    assert_rejected,
    create_waiting,
    dispatch,
    join,
    player,
    public_facts,
)


def test_host_transfer_to_self_is_rejected_without_mutation() -> None:
    """TSK-266 11.2：把局主转让给自己 → ``invalid_transfer_target`` + ``self``。

    "把局主转让给自己（`invalid_transfer_target` + `self`）：不能把局主转让给自己。"
    普通错误不返回局面，提交结果必须保持原状（不推进 revision、不换局主）。
    """
    state = create_waiting(host=1)
    joined = join(state, 2)
    assert_ok(joined, "joined")
    state = joined.state

    before = public_facts(state)
    result = dispatch(state, Action.transfer(player(1), 1))

    assert_rejected(result, "invalid_transfer_target", "self")
    assert public_facts(result.state) == before
    assert result.state.host_seq == 1


def test_host_transfer_to_other_player_still_succeeds() -> None:
    """回归：合法的转让仍能成功，自转让拒绝不误伤正常路径。"""
    state = create_waiting(host=1)
    joined = join(state, 2)
    assert_ok(joined, "joined")
    state = joined.state

    result = dispatch(state, Action.transfer(player(1), 2))

    assert_ok(result, "host_transferred")
    assert result.state.host_seq == 2
