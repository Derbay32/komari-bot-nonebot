"""TSK-278 RED baseline: QQ button keyboard seam.

The red root for this file is the missing top-level ``build_keyboard`` symbol.
Assertions follow ``TSK-278-contract.md`` section 5 and the TSK-266 1H /
6a9e6ce3 confirmed layouts: emoji labels, at most 3 buttons per row,
action.type=2 / permission.type=2 / reply=false / enter=false, fill-only
commands with a trailing space where an argument is expected.
"""

from __future__ import annotations

from typing import Any

from komari_bot.plugins.komari_roulette import (
    ReplyProjectionContext,
    build_keyboard,
)

from .tsk278_support import (
    context,
    flatten_keyboard,
    game_view,
    player,
)


def _labels(rows: list[list[Any]]) -> list[list[str]]:
    return [[button.label for button in row] for row in rows]


def _data(rows: list[list[Any]]) -> list[list[str]]:
    return [[button.data for button in row] for row in rows]


def _assert_fill_only(rows: list[list[Any]]) -> None:
    for row in rows:
        assert len(row) <= 3, "at most 3 buttons per row"
        for button in row:
            assert button.action_type == 2, button
            assert button.permission_type == 2, button
            assert button.reply is False, button
            assert button.enter is False, button
            assert button.data, "button data must not be empty"


def _follow_up_context() -> ReplyProjectionContext:
    return context(
        result_code="shot",
        lifecycle="active",
        phase="follow_up",
        players=(
            player(2, name="小红", inventory=(("beer", 1), ("lock", 1))),
            player(1, name="小明", inventory=(("magnifier", 1),)),
            player(3, name="小白", alive=False),
        ),
        current_player=player(1, name="小明"),
        view=game_view(remaining_total=4, remaining_live=1, remaining_blank=3, hit_percent=25.0),
    )


def test_follow_up_button_layout_1h() -> None:
    rows = flatten_keyboard(build_keyboard(_follow_up_context()))
    assert _labels(rows) == [
        ["🧰使用", "🗑️丢弃"],
        ["🔫开枪", "🔄装填"],
        ["⏹️结束", "🏳️弃权"],
    ]
    _assert_fill_only(rows)
    assert _data(rows)[0] == ["/轮盘 道具 使用 ", "/轮盘 道具 丢弃 "]
    assert _data(rows)[1] == ["/轮盘 开枪", "/轮盘 装填"]
    assert _data(rows)[2] == ["/轮盘 结束", "/轮盘 弃权"]


def test_locked_turn_buttons() -> None:
    base = context(
        result_code="shot",
        lifecycle="active",
        phase="locked",
        players=(player(1, name="小明"), player(2, name="小红")),
        current_player=player(1, name="小明"),
        view=game_view(
            remaining_total=4,
            remaining_live=1,
            remaining_blank=3,
            hit_percent=25.0,
            active_lock_player=player(1, name="小明"),
        ),
    )
    rows = flatten_keyboard(build_keyboard(base))
    assert _labels(rows) == [["🔫开枪", "🏳️弃权"]]
    _assert_fill_only(rows)
    assert _data(rows)[0] == ["/轮盘 开枪", "/轮盘 弃权"]


def test_item_choice_buttons() -> None:
    base = context(
        result_code="item_choice_pending",
        lifecycle="active",
        phase="item_choice",
        details={"pending_item": "magnifier"},
        players=(player(1, name="小明", inventory=(("magnifier", 1), ("beer", 2), ("lock", 1))),),
        current_player=player(1, name="小明"),
        reward_player=player(1, name="小明"),
        view=game_view(
            remaining_total=4,
            remaining_live=1,
            remaining_blank=3,
            hit_percent=25.0,
            pending_reward_count=1,
        ),
    )
    rows = flatten_keyboard(build_keyboard(base))
    assert _labels(rows) == [
        ["🗑️丢弃新道具", "🏳️弃权"],
        ["🔄替换放大镜", "🔄替换啤酒"],
        ["🔄替换锁"],
    ]
    _assert_fill_only(rows)
    assert _data(rows)[0] == ["/轮盘 奖励 丢弃", "/轮盘 弃权"]
    assert _data(rows)[1] == ["/轮盘 奖励 替换 放大镜", "/轮盘 奖励 替换 啤酒"]
    assert _data(rows)[2] == ["/轮盘 奖励 替换 锁"]


def test_waiting_buttons_with_transfer() -> None:
    base = context(
        result_code="joined",
        lifecycle="waiting",
        phase=None,
        players=(player(1, name="小明"), player(2, name="小红")),
        current_player=None,
        view=game_view(host_seq=1, remaining_total=4, remaining_live=1, remaining_blank=3, hit_percent=25.0),
    )
    rows = flatten_keyboard(build_keyboard(base))
    # 加入/退出/开始/取消 + 通用 🔄转让（末尾保留空格）。
    labels = _labels(rows)
    assert "🔄转让" in [b for row in labels for b in row]
    assert all("加入" in b or "退出" in b or "开始" in b or "取消" in b for b in labels[0])
    _assert_fill_only(rows)
    transfer = [b for row in rows for b in row if b.label == "🔄转让"]
    assert len(transfer) == 1
    assert transfer[0].data == "/轮盘 转让 "


def test_waiting_solo_host_omits_transfer() -> None:
    base = context(
        result_code="created",
        lifecycle="waiting",
        phase=None,
        players=(player(1, name="小明"),),
        current_player=None,
        view=game_view(host_seq=1, remaining_total=4, remaining_live=1, remaining_blank=3, hit_percent=25.0),
    )
    rows = flatten_keyboard(build_keyboard(base))
    labels = [b for row in _labels(rows) for b in row]
    assert "🔄转让" not in labels


def test_final_has_no_buttons() -> None:
    base = context(
        result_code="shot",
        lifecycle="completed",
        phase=None,
        winner=player(1, name="小明"),
        winner_group_wins=1,
        players=(player(1, name="小明"),),
        view=game_view(remaining_total=0, remaining_live=0, remaining_blank=0, hit_percent=None),
    )
    keyboard = build_keyboard(base)
    assert len(list(keyboard.rows)) == 0


def test_error_has_no_buttons() -> None:
    base = context(
        result_code="not_current_player",
        game_id=None,
        lifecycle=None,
        phase=None,
        state_revision=None,
        turn_seq=None,
        actor_member_openid="member-9",
        target_mention_count=0,
        details={},
    )
    keyboard = build_keyboard(base)
    assert len(list(keyboard.rows)) == 0


def test_keyboard_is_pure_over_frozen_context() -> None:
    base = _follow_up_context()
    first = flatten_keyboard(build_keyboard(base))
    second = flatten_keyboard(build_keyboard(base))
    assert first == second
